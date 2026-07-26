"""The WebSocket backpressure policy, tested where it is deterministic.

``Subscription`` is the one place the engine's threads meet the event loop, and
its contract is narrow but load-bearing:

* :meth:`Subscription.offer` is called from a storage thread and must never
  block, never raise, and never wait on a consumer;
* when the queue is full the **oldest** event is discarded, because a live
  visualizer cares about now, not about a backlog from ten seconds ago;
* every discard is counted and surfaced, so the UI can show a gap rather than
  implying it saw everything.

Driving this through an HTTP test client cannot exercise the overflow branch —
its transport buffers without limit — so the policy is tested directly here.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from engine.diagnostics import EventCategory, PageReadEvent, TraceLevel, TraceRecord
from engine.server.routers.events import Subscription

QUEUE_SIZE = 8


def make_record(seq: int) -> TraceRecord:
    return TraceRecord(
        seq=seq,
        timestamp_ns=seq,
        category=EventCategory.STORAGE,
        level=TraceLevel.STORAGE,
        event_type="PageReadEvent",
        event=PageReadEvent(
            page_id=seq, file_offset=seq * 4096, source="disk", duration_ns=1
        ),
    )


def run(coro):
    """Run one coroutine on a fresh loop and clean it up."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_events_within_capacity_are_all_delivered():
    async def scenario() -> list[TraceRecord]:
        subscription = Subscription(asyncio.get_running_loop(), QUEUE_SIZE)
        for seq in range(1, 5):
            subscription._enqueue(make_record(seq))
        return await subscription.drain(max_batch=QUEUE_SIZE)

    batch = run(scenario())
    assert [item.seq for item in batch] == [1, 2, 3, 4]


def test_overflow_discards_the_oldest_and_keeps_the_newest():
    async def scenario() -> tuple[list[TraceRecord], int]:
        subscription = Subscription(asyncio.get_running_loop(), QUEUE_SIZE)
        for seq in range(1, 21):  # 20 events into a queue of 8
            subscription._enqueue(make_record(seq))
        batch = await subscription.drain(max_batch=QUEUE_SIZE)
        return batch, subscription.take_dropped()

    batch, dropped = run(scenario())
    assert [item.seq for item in batch] == [13, 14, 15, 16, 17, 18, 19, 20]
    assert dropped == 12


def test_the_drop_counter_resets_once_reported():
    async def scenario() -> tuple[int, int]:
        subscription = Subscription(asyncio.get_running_loop(), 2)
        for seq in range(1, 6):
            subscription._enqueue(make_record(seq))
        first = subscription.take_dropped()
        second = subscription.take_dropped()
        return first, second

    first, second = run(scenario())
    assert first == 3
    # Reported once; the client is not told about the same gap twice.
    assert second == 0


def test_drain_coalesces_up_to_the_batch_size():
    async def scenario() -> tuple[int, int]:
        subscription = Subscription(asyncio.get_running_loop(), QUEUE_SIZE)
        for seq in range(1, 7):
            subscription._enqueue(make_record(seq))
        first = await subscription.drain(max_batch=4)
        second = await subscription.drain(max_batch=4)
        return len(first), len(second)

    assert run(scenario()) == (4, 2)


def test_drain_waits_for_the_first_event_rather_than_spinning():
    async def scenario() -> list[int]:
        loop = asyncio.get_running_loop()
        subscription = Subscription(loop, QUEUE_SIZE)
        loop.call_later(0.01, subscription._enqueue, make_record(42))
        batch = await asyncio.wait_for(subscription.drain(max_batch=4), timeout=2)
        return [item.seq for item in batch]

    assert run(scenario()) == [42]


def test_offer_from_another_thread_reaches_the_loop():
    """The real path: a storage thread hands an event to the event loop."""
    results: list[int] = []
    ready = threading.Event()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        subscription = Subscription(loop, QUEUE_SIZE)

        def producer() -> None:
            ready.wait(timeout=2)
            for seq in range(1, 4):
                subscription.offer(make_record(seq))

        thread = threading.Thread(target=producer)
        thread.start()
        ready.set()
        # drain() returns as soon as it has at least one event, by design — it
        # must not wait for a batch to fill. So keep draining until all three
        # have arrived rather than assuming they land in one call.
        while len(results) < 3:
            batch = await asyncio.wait_for(subscription.drain(max_batch=3), timeout=2)
            results.extend(item.seq for item in batch)
        thread.join(timeout=2)

    run(scenario())
    assert results == [1, 2, 3]


def test_offer_never_blocks_even_when_the_queue_is_saturated():
    """A producer must not be slowed by a consumer that has stopped reading."""

    async def scenario() -> int:
        subscription = Subscription(asyncio.get_running_loop(), 4)
        # Ten thousand events into a four-slot queue, with nobody draining.
        for seq in range(10_000):
            subscription._enqueue(make_record(seq))
        return subscription.total_dropped

    assert run(scenario()) == 10_000 - 4


def test_offering_after_close_is_a_no_op():
    async def scenario() -> int:
        subscription = Subscription(asyncio.get_running_loop(), QUEUE_SIZE)
        subscription.close()
        subscription.offer(make_record(1))
        return subscription._queue.qsize()

    assert run(scenario()) == 0


def test_offer_survives_a_closed_event_loop():
    """A client can vanish between an event being emitted and delivered."""
    loop = asyncio.new_event_loop()
    subscription = Subscription(loop, QUEUE_SIZE)
    loop.close()
    subscription.offer(make_record(1))  # must not raise


@pytest.mark.parametrize("queue_size", [1, 2, 64])
def test_capacity_is_respected_exactly(queue_size: int):
    async def scenario() -> int:
        subscription = Subscription(asyncio.get_running_loop(), queue_size)
        for seq in range(queue_size * 3):
            subscription._enqueue(make_record(seq))
        return subscription._queue.qsize()

    assert run(scenario()) == queue_size
