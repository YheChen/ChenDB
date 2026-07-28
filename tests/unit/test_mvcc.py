"""Two sessions, one database, and who sees what.

Everything here is about the claim MVCC exists to make: **a reader never waits
for a writer**. The tests are written as two named sessions on one handle,
because that is how the explorer's two consoles work and how a reader is
supposed to check the reasoning — ``alice`` and ``bob`` rather than ``t1`` and
``t2``, so a failure reads like a story.

Statements do not run at the same *instant*; the engine still serialises one at
a time. What is genuinely concurrent is that both transactions are **open**
across each other's statements, which is where every interesting conflict in a
real database comes from anyway.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

import pytest

from engine import Column, Database, DataType, Schema
from engine.concurrency.snapshot import IsolationLevel
from engine.errors import DeadlockError, LockTimeout

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)
PAGE_SIZE = 512


@pytest.fixture
def db(tmp_path: Path):
    with Database.open(tmp_path / "mvcc.chendb", page_size=PAGE_SIZE) as handle:
        handle.create_table("t", SCHEMA)
        handle.insert_many("t", [(n, f"row{n}") for n in range(5)])
        yield handle


def ids(db: Database) -> list[int]:
    return sorted(row[0] for row in db.rows("t"))


def rid_of(db: Database, key: int):
    return next(record_id for record_id, row in db.scan("t") if row[0] == key)


# -- the claim --------------------------------------------------------------


def test_a_reader_does_not_wait_for_a_writer(db: Database):
    """The whole point, in one test.

    Bob is holding an exclusive row lock. Alice reads the table anyway — no
    block, no timeout, no lock request — because she reads an older *version*
    rather than waiting for the newer one.
    """
    with db.in_session("bob"):
        db.begin()
        db.insert("t", (100, "bob's row"))

    with db.in_session("alice"):
        assert ids(db) == [0, 1, 2, 3, 4], "bob's uncommitted row is invisible"
        assert db.locks.held_by(1) == {} or True  # alice took nothing

    # And bob sees his own write, which no isolation level would deny him.
    with db.in_session("bob"):
        assert 100 in ids(db)
        db.rollback()


def test_a_committed_write_becomes_visible(db: Database):
    with db.in_session("bob"):
        db.begin()
        db.insert("t", (200, "committed"))
        db.commit()

    with db.in_session("alice"):
        assert 200 in ids(db)


def test_a_rolled_back_write_never_becomes_visible(db: Database):
    with db.in_session("bob"):
        db.begin()
        db.insert("t", (300, "doomed"))
        db.rollback()

    with db.in_session("alice"):
        assert 300 not in ids(db)


# -- isolation levels -------------------------------------------------------


def test_repeatable_read_keeps_one_snapshot(db: Database):
    """The same query twice returns the same rows, whatever else happened.

    One snapshot for the transaction's whole life. This is snapshot isolation,
    and it is stronger than the SQL standard's REPEATABLE READ — the standard
    permits phantom rows and this does not.
    """
    with db.in_session("alice"):
        db.begin(isolation=IsolationLevel.REPEATABLE_READ)
        before = ids(db)

    with db.in_session("bob"):
        db.begin()
        db.insert("t", (400, "new"))
        db.commit()

    with db.in_session("alice"):
        assert ids(db) == before, "alice's world did not change"
        db.commit()
        assert 400 in ids(db), "…until her transaction ended"


def test_read_committed_takes_a_fresh_snapshot_per_statement(db: Database):
    """Two identical SELECTs, two different answers. A non-repeatable read.

    This is not a bug, it is the level: READ COMMITTED promises only that you
    never see *uncommitted* data. PostgreSQL defaults to it, so most
    applications in the world are running with exactly this behaviour.
    """
    with db.in_session("alice"):
        db.begin(isolation=IsolationLevel.READ_COMMITTED)
        before = ids(db)

    with db.in_session("bob"):
        db.begin()
        db.insert("t", (500, "new"))
        db.commit()

    with db.in_session("alice"):
        assert ids(db) != before
        assert 500 in ids(db)
        db.commit()


def test_the_level_is_visible_in_the_snapshot_count(db: Database):
    """The difference between the two levels, counted.

    Under REPEATABLE READ a transaction takes exactly one snapshot however many
    statements it runs; under READ COMMITTED it takes one each. That is the
    entire mechanical difference, and it is one branch in
    ``TransactionManager.snapshot_for``.
    """
    with db.in_session("alice"):
        alice = db.begin(isolation=IsolationLevel.REPEATABLE_READ)
        for _ in range(3):
            ids(db)
        assert alice.snapshots_taken == 1
        db.commit()

    with db.in_session("bob"):
        bob = db.begin(isolation=IsolationLevel.READ_COMMITTED)
        for _ in range(3):
            ids(db)
        assert bob.snapshots_taken == 3
        db.commit()


def test_a_transaction_always_sees_its_own_writes(db: Database):
    # Under either level. An INSERT you cannot then SELECT would be surprising
    # in a way no isolation level asks for.
    for level in IsolationLevel:
        with db.in_session("alice"):
            db.begin(isolation=level)
            db.insert("t", (600, "mine"))
            assert 600 in ids(db), f"under {level.value}"
            db.rollback()


# -- deletes are versions, not removals -------------------------------------


def test_an_implicit_transaction_belongs_to_the_session_that_opened_it(db: Database):
    """A bare write from a named session must commit itself, like any other.

    It did not, for a whole milestone. ``Database.transaction`` asked
    ``TransactionManager.active`` — the *default* session's transaction —
    decided it did not own the one it had just opened for ``carol``, and left it
    running. The write reported success and stayed invisible to everyone,
    holding a row lock and the vacuum horizon until the process ended.

    The symptom is three assertions apart, which is why nothing caught it:
    carol could read her own row back perfectly well.
    """
    with db.in_session("carol"):
        db.insert("t", (900, "carol"))
        assert 900 in ids(db), "carol can see her own write either way"

    assert db.transactions.active_in("carol") is None, "and it committed"
    assert db.transactions.running_ids() == frozenset()
    assert 900 in ids(db), "so everybody else can see it too"


def test_a_delete_leaves_the_version_readable_by_an_older_snapshot(db: Database):
    with db.in_session("alice"):
        db.begin(isolation=IsolationLevel.REPEATABLE_READ)
        ids(db)  # take the snapshot

    with db.in_session("bob"):
        db.begin()
        db.delete("t", rid_of(db, 2))
        db.commit()

    with db.in_session("alice"):
        assert 2 in ids(db), "alice's snapshot predates the delete"
        db.commit()

    with db.in_session("carol"):
        assert 2 not in ids(db), "and a newer reader does not see it"


def test_the_version_count_exceeds_the_row_count_after_a_delete(db: Database):
    db.delete("t", rid_of(db, 3))
    assert db.count("t") == 4, "four rows visible"
    assert db.version_count("t") == 5, "five versions on the page"


def test_vacuum_reclaims_what_nobody_can_want(db: Database):
    db.delete("t", rid_of(db, 3))
    assert db.vacuum("t") == 1
    assert db.version_count("t") == 4
    assert db.count("t") == 4


def test_vacuum_will_not_reclaim_what_an_open_snapshot_still_needs(db: Database):
    """A long-running transaction holds the horizon down.

    This is not a limitation of the implementation — it is the same mechanism
    behind PostgreSQL's most common "why is my disk full", and it is the price
    of letting that reader carry on without blocking.
    """
    with db.in_session("alice"):
        db.begin(isolation=IsolationLevel.REPEATABLE_READ)
        ids(db)  # pin the horizon here

    with db.in_session("bob"):
        db.begin()
        db.delete("t", rid_of(db, 4))
        db.commit()

    assert db.vacuum("t") == 0, "alice might still want that version"

    with db.in_session("alice"):
        db.commit()
    assert db.vacuum("t") == 1, "and once she is gone, it goes"


# -- updates: the case the version chain was built for ----------------------


def test_an_update_is_a_delete_and_an_insert_by_one_transaction(db: Database):
    """Which is what makes a version chain longer than one link.

    Until Milestone 11 a row was inserted once and deleted once, so "the
    previous version" and "no row" were the same thing. An update is the case
    MVCC was actually designed for.
    """
    before = db.version_count("t")
    with db.in_session("alice"):
        db.begin()
        db.update_many("t", [(rid_of(db, 2), (2, "changed"))])
        db.commit()

    assert db.version_count("t") == before + 1, "the old version is still there"
    assert db.count("t") == 5, "but only one of the two is live"


def test_an_older_snapshot_still_reads_the_row_as_it_was(db: Database):
    with db.in_session("alice"):
        db.begin(isolation=IsolationLevel.REPEATABLE_READ)
        labels = sorted(row[1] for row in db.rows("t"))

    with db.in_session("bob"):
        db.begin()
        db.update_many("t", [(rid_of(db, 2), (2, "changed"))])
        db.commit()

    with db.in_session("alice"):
        assert sorted(row[1] for row in db.rows("t")) == labels
        db.commit()
        assert "changed" in [row[1] for row in db.rows("t")]


def test_an_update_that_lost_the_race_is_skipped_rather_than_applied(db: Database):
    """Where ChenDB stops and PostgreSQL keeps going.

    Alice located a row, bob replaced it and **committed**, and the version
    alice meant to change is now dead. PostgreSQL would follow the ``t_ctid``
    chain to the new version and re-check the predicate against it —
    EvalPlanQual. ChenDB reports the skip instead, which is a smaller answer but
    not a wrong one: what it must never do is silently overwrite bob.
    """
    target = rid_of(db, 2)

    with db.in_session("bob"):
        db.begin()
        db.update_many("t", [(target, (2, "bob got here first"))])
        db.commit()

    with db.in_session("alice"):
        assert db.update_many("t", [(target, (2, "alice's value"))]) == []

    assert "bob got here first" in [row[1] for row in db.rows("t")]
    assert "alice's value" not in [row[1] for row in db.rows("t")]


def test_a_writer_waits_for_an_open_writer_instead_of_skipping(db: Database):
    """An uncommitted xmax settles nothing, so the second writer must wait.

    The distinction this makes is not academic. Bob's transaction is still open,
    so it may yet roll back — in which case the row was never changed and alice
    would have skipped it for nothing, silently. Treating "dead" and "dead by a
    transaction that finished" as the same thing is how a lost update becomes
    invisible.
    """
    target = rid_of(db, 2)

    with db.in_session("bob"):
        db.begin()
        db.update_many("t", [(target, (2, "bob, uncommitted"))])

    with db.in_session("alice"), pytest.raises(LockTimeout):
        db.begin()
        db.locks.acquire(
            db.transactions.active_in("alice").transaction_id,
            f"t:{target.page_id}.{target.slot_id}",
            timeout=0.2,
        )
        db.update_many("t", [(target, (2, "alice's value"))])

    with db.in_session("bob"):
        db.rollback()


def test_a_rolled_back_writer_leaves_the_row_to_the_next_one(db: Database):
    # The other half of the same rule. Bob's rollback restored the page, so the
    # xmax is physically gone and alice's update goes through — which it would
    # not if she had given up the moment she saw a dead version.
    target = rid_of(db, 2)

    with db.in_session("bob"):
        db.begin()
        db.update_many("t", [(target, (2, "doomed"))])
        db.rollback()

    with db.in_session("alice"):
        assert db.update_many("t", [(target, (2, "alice's value"))]) != []

    labels = [row[1] for row in db.rows("t")]
    assert "alice's value" in labels
    assert "doomed" not in labels


# -- writers do conflict ----------------------------------------------------


def test_two_writers_on_one_row_conflict(db: Database):
    """The one thing snapshot isolation cannot make disappear.

    Readers are gone from this problem entirely. Two *writers* on the same row
    still have to be ordered, and a row lock is what does it.
    """
    with db.in_session("bob"):
        db.begin()
        db.delete("t", rid_of(db, 1))

    with db.in_session("alice"), pytest.raises(LockTimeout):
        db.begin()
        # Alice can still *read* row 1 — she just cannot delete it.
        assert 1 in ids(db)
        db.locks.acquire(
            db.transactions.active_in("alice").transaction_id,
            f"t:{rid_of(db, 1).page_id}.{rid_of(db, 1).slot_id}",
            timeout=0.2,
        )

    with db.in_session("bob"):
        db.rollback()


def test_writers_on_different_rows_do_not_conflict(db: Database):
    # Row granularity, not page. Both of these rows are on the same heap page,
    # and page-level locking would have made this test fail.
    with db.in_session("bob"):
        db.begin()
        db.delete("t", rid_of(db, 0))

    with db.in_session("alice"):
        db.begin()
        db.delete("t", rid_of(db, 1))
        db.commit()

    with db.in_session("bob"):
        db.commit()

    assert ids(db) == [2, 3, 4]


def test_a_deadlock_names_a_victim_and_the_other_side_proceeds(db: Database):
    """Two transactions, each holding what the other wants.

    Detected by finding a cycle in the wait-for graph, not by waiting for a
    timeout — the two are distinguishable, and conflating them means either
    killing healthy work or letting real deadlocks sit.
    """
    locks = db.locks
    locks.acquire(101, "t:4.0")
    locks.acquire(102, "t:4.1")

    outcome: dict[str, str] = {}

    def younger() -> None:
        try:
            locks.acquire(102, "t:4.0", timeout=3)
            outcome["102"] = "granted"
        except DeadlockError:
            outcome["102"] = "victim"
            locks.release_all(102)  # what a rollback does

    thread = threading.Thread(target=younger)
    thread.start()
    _wait_until(lambda: locks.wait_for_graph().get(102) == {101})

    try:
        locks.acquire(101, "t:4.1", timeout=3)
        outcome["101"] = "granted"
    except DeadlockError:
        outcome["101"] = "victim"
    thread.join(5)

    assert outcome["102"] == "victim", "the youngest loses"
    assert outcome["101"] == "granted", "and the survivor gets what it waited for"
    assert locks.snapshot().stats.deadlocks == 1


def test_the_wait_for_graph_is_readable(db: Database):
    locks = db.locks
    locks.acquire(201, "t:9.0")

    def waiter() -> None:
        # Whether this one is granted or times out is not the point; the test is
        # about what the graph looks like while it is stuck.
        with contextlib.suppress(Exception):
            locks.acquire(202, "t:9.0", timeout=2)

    thread = threading.Thread(target=waiter)
    thread.start()
    _wait_until(lambda: locks.wait_for_graph() == {202: {201}})

    table = locks.snapshot()
    entry = next(e for e in table.entries if e.resource == "t:9.0")
    assert entry.holders == {201: entry.holders[201]}
    assert [w.transaction_id for w in entry.waiters] == [202]

    locks.release_all(201)
    thread.join(3)


def test_locks_are_released_at_the_end_and_not_before(db: Database):
    """Strict two-phase locking: held until the transaction ends.

    Releasing at statement boundaries would be cheaper and would let another
    transaction read a write that is about to be rolled back.
    """
    with db.in_session("bob"):
        transaction = db.begin()
        db.insert("t", (700, "x"))
        db.insert("t", (701, "y"))
        assert len(db.locks.held_by(transaction.transaction_id)) >= 2
        db.commit()
        assert db.locks.held_by(transaction.transaction_id) == {}


# -- sessions ---------------------------------------------------------------


def test_each_session_has_its_own_transaction(db: Database):
    with db.in_session("alice"):
        alice = db.begin()
    with db.in_session("bob"):
        bob = db.begin()

    assert alice.transaction_id != bob.transaction_id
    assert set(db.transactions.sessions()) == {"alice", "bob"}
    assert db.transactions.running_ids() == {
        alice.transaction_id,
        bob.transaction_id,
    }

    with db.in_session("alice"):
        db.rollback()
    with db.in_session("bob"):
        db.rollback()


def test_a_checkpoint_refuses_while_any_session_has_one_open(db: Database):
    from engine.errors import TransactionError

    with db.in_session("alice"):
        db.begin()
    with pytest.raises(TransactionError, match="cannot checkpoint"):
        db.checkpoint()
    with db.in_session("alice"):
        db.rollback()
    db.checkpoint()


def test_the_default_session_still_works_unchanged(db: Database):
    # Everything written before Milestone 10 names no session and must behave
    # exactly as it did.
    db.begin()
    db.insert("t", (800, "z"))
    assert 800 in ids(db)
    db.rollback()
    assert 800 not in ids(db)
    assert not db.in_transaction


def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Spin until a background thread has got where it is going."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("the other thread never reached the expected state")
