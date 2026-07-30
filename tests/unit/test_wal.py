"""The log, its records, and the three recovery passes.

Recovery is tested against a **dictionary** rather than a database file, because
that is the whole point of the callback boundary: ``recover`` never learns what
a page is, so its argument is a ``dict[int, bytes]`` here and a pager in
production, and the algorithm is the same either way.

The crash tests in ``tests/recovery/`` cover the other direction (a real
process killed with ``SIGKILL``) and neither replaces the other. These say the
algorithm is right; those say it is wired up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.errors import TransactionError
from engine.wal.log import WAL_SUFFIX, WriteAheadLog
from engine.wal.record import (
    NO_TRANSACTION,
    RECORD_HEADER_SIZE,
    LogRecord,
    RecordType,
    decode_record,
)
from engine.wal.recovery import recover

PAGE = 512


def page(fill: int) -> bytes:
    return bytes([fill]) * PAGE


@pytest.fixture
def log(tmp_path: Path) -> WriteAheadLog:
    with WriteAheadLog(tmp_path / "t.chendb-wal") as handle:
        yield handle


# -- records ----------------------------------------------------------------


def test_a_record_round_trips():
    record = LogRecord(
        lsn=0,
        prev_lsn=0,
        transaction_id=7,
        record_type=RecordType.UPDATE,
        page_id=4,
        before_image=page(1),
        after_image=page(2),
    )
    back = decode_record(record.to_bytes(), 0)
    assert back == record


def test_the_lsn_is_the_byte_offset():
    """Not a counter. The whole design leans on this.

    "Durable up to LSN n" becomes "the first n bytes are on disk", and a
    record's LSN is knowable before it is written, which the pager needs,
    because the page inside the record has to carry it.
    """
    first = LogRecord(0, 0, 1, RecordType.UPDATE, 4, b"", page(1))
    assert first.end_lsn == first.size == RECORD_HEADER_SIZE + PAGE
    second = LogRecord(first.end_lsn, first.lsn, 1, RecordType.COMMIT)
    assert decode_record(first.to_bytes() + second.to_bytes(), first.end_lsn) == second


def test_a_truncated_record_decodes_to_none():
    # The normal state of a log after a crash, not corruption.
    raw = LogRecord(0, 0, 1, RecordType.UPDATE, 4, b"", page(1)).to_bytes()
    assert decode_record(raw[: len(raw) - 40], 0) is None
    assert decode_record(raw[:10], 0) is None


def test_a_corrupted_record_decodes_to_none():
    raw = bytearray(LogRecord(0, 0, 1, RecordType.UPDATE, 4, b"", page(1)).to_bytes())
    raw[RECORD_HEADER_SIZE + 20] ^= 0xFF
    assert decode_record(bytes(raw), 0) is None


def test_a_record_found_at_the_wrong_offset_is_rejected():
    """The check that makes a stale log after a checkpoint read as empty.

    A checkpoint truncates the file and moves the base LSN. If the meta page's
    new base reaches the disk but the truncation does not, the next open reads
    old records at a base they were not written for, and this is what stops
    them being replayed.
    """
    raw = LogRecord(0, 0, 1, RecordType.UPDATE, 4, b"", page(1)).to_bytes()
    assert decode_record(raw, 0, base_lsn=0) is not None
    assert decode_record(raw, 0, base_lsn=4096) is None


def test_an_unknown_record_type_decodes_to_none():
    raw = bytearray(LogRecord(0, 0, 1, RecordType.COMMIT).to_bytes())
    raw[28] = 99
    # Re-checksum so the type is what fails, not the CRC.
    import struct
    import zlib

    struct.pack_into("<I", raw, 0, zlib.crc32(memoryview(raw)[4:]))
    assert decode_record(bytes(raw), 0) is None


# -- the log ----------------------------------------------------------------


def test_appending_advances_the_next_lsn(log: WriteAheadLog):
    assert log.next_lsn == 0
    record = log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(1))
    assert log.next_lsn == record.end_lsn


def test_records_are_staged_until_flushed(log: WriteAheadLog):
    log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(1))
    assert log.flushed_lsn == 0, "nothing has reached the OS"
    assert log.buffered_bytes > 0
    log.flush()
    assert log.flushed_lsn == log.next_lsn
    assert log.buffered_bytes == 0


def test_the_staged_total_is_tracked_rather_than_recomputed(log: WriteAheadLog):
    """``next_lsn`` is read once per append, so it must not walk the buffer.

    It used to, and a transaction of *n* records therefore cost O(n squared):
    one 2,000-row ``UPDATE`` spent 8.5 of its 9.8 seconds inside that ``sum``.
    The invariant is cheap to check and the regression is not.
    """
    expected = 0
    for n in range(200):
        record = log.append(
            RecordType.UPDATE, transaction_id=1, page_id=n, after_image=page(n)
        )
        expected += record.size
        assert log.buffered_bytes == expected
        assert log.next_lsn == expected

    # And coalescing, which replaces the tail in place rather than appending.
    before = log.buffered_bytes
    log.append_update(
        transaction_id=1,
        page_id=199,
        before_image=b"",
        after_image_for=lambda _: page(7),
    )
    assert log.stats.records_coalesced == 1
    assert log.buffered_bytes == before, "same-size image, same total"

    log.flush()
    assert log.flushed_lsn == expected
    assert log.buffered_bytes == 0


def test_the_transaction_chain_is_maintained(log: WriteAheadLog):
    first = log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(1))
    log.append(RecordType.UPDATE, transaction_id=2, page_id=3, after_image=page(1))
    third = log.append(RecordType.UPDATE, transaction_id=1, page_id=4, after_image=page(1))

    assert first.prev_lsn == 0
    assert third.prev_lsn == first.lsn, "chained to the transaction, not the log"


def test_a_commit_is_synced(log: WriteAheadLog):
    log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(1))
    log.commit(1)
    assert log.buffered_bytes == 0, "a commit that is still in memory is not a commit"
    assert log.stats.syncs == 1


def test_reading_back_what_was_written(log: WriteAheadLog):
    log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(1))
    log.commit(1)
    records, truncated = log.read_all()
    assert not truncated
    assert [r.record_type for r in records] == [RecordType.UPDATE, RecordType.COMMIT]


def test_a_torn_tail_is_reported_not_raised(tmp_path: Path):
    path = tmp_path / ("torn" + WAL_SUFFIX)
    with WriteAheadLog(path) as log:
        log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(1))
        log.append(RecordType.UPDATE, transaction_id=1, page_id=3, after_image=page(2))
        log.flush()

    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) - 100])

    with WriteAheadLog(path) as log:
        records, truncated = log.read_all()
    assert truncated
    assert len(records) == 1, "the whole record survives, the partial one does not"


# -- coalescing -------------------------------------------------------------


def test_consecutive_writes_to_one_page_coalesce(log: WriteAheadLog):
    """A bulk insert writes the same heap page row after row, and only the last
    image matters, redo replays them in order and each overwrites the last.
    """
    images = [page(n) for n in range(1, 4)]
    for image in images:
        log.append_update(
            transaction_id=1,
            page_id=5,
            before_image=b"",
            after_image_for=lambda _, i=image: i,
        )

    log.flush()
    records, _ = log.read_all()
    assert len(records) == 1, "three writes, one record"
    assert records[0].after_image == images[-1], "and it holds the newest image"
    assert log.stats.records_coalesced == 2


def test_coalescing_keeps_the_first_before_image(log: WriteAheadLog):
    # The before-image is the state at the transaction's *first* touch. A later
    # write to the same page does not change what a rollback should restore.
    log.append_update(
        transaction_id=1, page_id=5, before_image=page(9), after_image_for=lambda _: page(1)
    )
    log.append_update(
        transaction_id=1, page_id=5, before_image=b"", after_image_for=lambda _: page(2)
    )
    log.flush()
    (record,) = log.read_all()[0]
    assert record.before_image == page(9)
    assert record.after_image == page(2)


def test_a_different_page_breaks_the_run(log: WriteAheadLog):
    for page_id in (5, 6, 5):
        log.append_update(
            transaction_id=1,
            page_id=page_id,
            before_image=b"",
            after_image_for=lambda _: page(1),
        )
    log.flush()
    assert len(log.read_all()[0]) == 3


def test_a_different_transaction_breaks_the_run(log: WriteAheadLog):
    for transaction_id in (1, 2):
        log.append_update(
            transaction_id=transaction_id,
            page_id=5,
            before_image=b"",
            after_image_for=lambda _: page(1),
        )
    log.flush()
    assert len(log.read_all()[0]) == 2


def test_a_flushed_record_is_never_coalesced_into(log: WriteAheadLog):
    """The safety condition. Once a record is on disk a page carrying its LSN
    may be on disk too, and rewriting the record behind that page would leave
    the two disagreeing.
    """
    log.append_update(
        transaction_id=1, page_id=5, before_image=b"", after_image_for=lambda _: page(1)
    )
    log.flush()
    log.append_update(
        transaction_id=1, page_id=5, before_image=b"", after_image_for=lambda _: page(2)
    )
    log.flush()
    assert len(log.read_all()[0]) == 2
    assert log.stats.records_coalesced == 0


def test_the_lsn_handed_to_the_callback_is_the_records_own(log: WriteAheadLog):
    # The page has to be stamped with the LSN of the record that ends up
    # carrying it: which, when coalescing, is the *superseded* record's.
    seen: list[int] = []

    def image(lsn: int) -> bytes:
        seen.append(lsn)
        return page(1)

    first = log.append_update(
        transaction_id=1, page_id=5, before_image=b"", after_image_for=image
    )
    second = log.append_update(
        transaction_id=1, page_id=5, before_image=b"", after_image_for=image
    )
    assert seen == [first.lsn, first.lsn] == [0, 0]
    assert second.lsn == first.lsn


# -- checkpoint -------------------------------------------------------------


def test_a_checkpoint_truncates_and_moves_the_base(log: WriteAheadLog):
    log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(1))
    log.commit(1)
    end = log.next_lsn

    flushed: list[str] = []
    record = log.checkpoint(flush_pages=lambda: flushed.append("pages"))

    assert flushed == ["pages"], "pages go down before the log is discarded"
    assert record.record_type is RecordType.CHECKPOINT
    assert log.base_lsn > end, "the stream continues past the checkpoint record"
    assert log.path.stat().st_size == 0
    assert log.read_all() == ([], False)


def test_lsns_keep_increasing_across_a_checkpoint(log: WriteAheadLog):
    """The reason ``base_lsn`` is persisted at all.

    If the stream restarted at 0, a record written after a checkpoint would
    carry an LSN below the one already stamped on a page, and redo, which
    skips a record whose LSN the page has passed, would skip it.
    """
    first = log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(1))
    log.checkpoint(flush_pages=lambda: None)
    second = log.append(RecordType.UPDATE, transaction_id=2, page_id=2, after_image=page(1))
    assert second.lsn > first.lsn


# -- recovery, against a dictionary ----------------------------------------


def build(log: WriteAheadLog, *, commit: bool) -> None:
    """One transaction touching two pages, optionally committed."""
    log.append(
        RecordType.UPDATE,
        transaction_id=1,
        page_id=1,
        before_image=page(0),
        after_image=page(0xAA),
    )
    log.append(
        RecordType.UPDATE,
        transaction_id=1,
        page_id=2,
        before_image=page(0),
        after_image=page(0xBB),
    )
    if commit:
        log.commit(1)
    log.flush()


def run(log: WriteAheadLog, disk: dict[int, bytes]):
    """Recover into a dictionary standing in for the database file.

    Pages here are raw fill bytes with no header, so there is no LSN to read:
    a page that is absent answers -1 and one that is present answers 0. That is
    the state a crash leaves (every record newer than every page) which is
    what makes redo do its work rather than skip it.
    """
    return recover(
        log,
        read_page_lsn=lambda page_id: 0 if page_id in disk else -1,
        apply_page=lambda page_id, image: disk.__setitem__(page_id, image),
    )


def test_an_empty_log_recovers_nothing(log: WriteAheadLog):
    report = run(log, {})
    assert not report.ran
    assert report.summary() == "clean shutdown; nothing to recover"


def test_a_committed_transaction_is_redone(log: WriteAheadLog):
    build(log, commit=True)
    disk: dict[int, bytes] = {}
    report = run(log, disk)

    assert report.winners == (1,)
    assert report.losers == ()
    assert disk == {1: page(0xAA), 2: page(0xBB)}
    assert report.pages_redone == 2
    assert report.pages_undone == 0
    assert report.clean


def test_an_uncommitted_transaction_is_undone(log: WriteAheadLog):
    build(log, commit=False)
    disk: dict[int, bytes] = {}
    report = run(log, disk)

    assert report.losers == (1,)
    assert disk == {1: page(0), 2: page(0)}, "back to the before-images"
    assert report.pages_undone == 2
    assert not report.clean


def test_repeating_history_redoes_losers_too(log: WriteAheadLog):
    """ARIES redoes everything and *then* undoes the losers.

    It looks like wasted work, and the alternative is worse: recovery would
    have to decide, per page, whether a loser's change reached the disk.
    """
    build(log, commit=False)
    disk: dict[int, bytes] = {}
    report = run(log, disk)
    assert report.pages_redone == 2, "redone first"
    assert report.pages_undone == 2, "then undone"


def test_an_aborted_transaction_is_treated_as_finished(log: WriteAheadLog):
    """A rollback wrote its restores through the ordinary page path, so they are
    already in the log as updates. Undoing again would be undoing the undo.
    """
    build(log, commit=False)
    log.append(RecordType.UPDATE, transaction_id=1, page_id=1, after_image=page(0))
    log.append(RecordType.UPDATE, transaction_id=1, page_id=2, after_image=page(0))
    log.abort(1)
    log.flush()

    disk: dict[int, bytes] = {}
    report = run(log, disk)
    assert report.winners == (1,)
    assert report.losers == ()
    assert report.pages_undone == 0
    assert disk == {1: page(0), 2: page(0)}


def test_a_record_the_page_already_has_is_skipped(log: WriteAheadLog):
    build(log, commit=True)
    disk: dict[int, bytes] = {}
    report = recover(
        log,
        read_page_lsn=lambda _page_id: 1 << 60,  # every page claims to be ahead
        apply_page=lambda page_id, image: disk.__setitem__(page_id, image),
    )
    assert report.pages_redone == 0
    assert report.pages_skipped == 2
    assert disk == {}, "nothing was written"


def test_undo_is_logged_so_it_can_be_interrupted(log: WriteAheadLog):
    """Compensation records. A machine that crashed once can crash again while
    recovering, and the completed part of an undo has to survive that.
    """
    build(log, commit=False)
    run(log, {})

    records, _ = log.read_all()
    tail = records[len(records) - 3 :]
    assert [r.record_type for r in tail] == [
        RecordType.UPDATE,
        RecordType.UPDATE,
        RecordType.ABORT,
    ]
    assert all(r.after_image == page(0) for r in tail[:2]), "the restores, logged"


def test_two_transactions_are_split_correctly(log: WriteAheadLog):
    log.append(
        RecordType.UPDATE,
        transaction_id=1,
        page_id=1,
        before_image=page(0),
        after_image=page(1),
    )
    log.commit(1)
    log.append(
        RecordType.UPDATE,
        transaction_id=2,
        page_id=2,
        before_image=page(0),
        after_image=page(2),
    )
    log.flush()

    disk: dict[int, bytes] = {}
    report = run(log, disk)
    assert report.winners == (1,)
    assert report.losers == (2,)
    assert disk == {1: page(1), 2: page(0)}


def test_records_outside_any_transaction_are_kept(log: WriteAheadLog):
    # Engine bookkeeping (the meta page written when a database is created)
    # belongs to nobody and has nobody to roll it back.
    log.append(
        RecordType.UPDATE, transaction_id=NO_TRANSACTION, page_id=0, after_image=page(7)
    )
    log.flush()
    disk: dict[int, bytes] = {}
    report = run(log, disk)
    assert report.losers == ()
    assert disk == {0: page(7)}


def test_the_report_summarises_itself(log: WriteAheadLog):
    build(log, commit=False)
    report = run(log, {})
    text = report.summary()
    assert "undone" in text and "interrupted" in text


# -- refusals ---------------------------------------------------------------


def test_a_closed_log_refuses_work(tmp_path: Path):
    log = WriteAheadLog(tmp_path / ("closed" + WAL_SUFFIX))
    log.close()
    with pytest.raises(ValueError, match="closed"):
        log.append(RecordType.COMMIT, transaction_id=1)


def test_closing_twice_is_fine(tmp_path: Path):
    log = WriteAheadLog(tmp_path / ("twice" + WAL_SUFFIX))
    log.close()
    log.close()
    assert log.closed


def test_a_checkpoint_with_a_transaction_open_is_refused(tmp_path: Path):
    from engine import Column, Database, DataType, Schema

    with Database.open(tmp_path / "open-txn.chendb", page_size=PAGE) as db:
        db.create_table(
            "t", Schema.of(Column("id", DataType.INTEGER, nullable=False, primary_key=True))
        )
        db.begin()
        db.insert("t", (1,))
        with pytest.raises(TransactionError, match="cannot checkpoint"):
            db.checkpoint()
        db.rollback()
        db.checkpoint()  # fine now
