"""Transactions.

The property under test is one sentence: **after a rollback every page the
database references is byte-for-byte what it was.** Not "the rows come back" —
byte-for-byte, because a physical undo either restores every page it touched or
it has a hole in it, and a hole shows up as corruption weeks later rather than
as a failing assertion now.

So the strongest tests here hash the file before and after. The one thing that
is *not* restored — trailing pages left by a transaction that extended the file
— is asserted directly rather than hidden by the helper; see :func:`digest` and
``test_a_rollback_may_leave_trailing_pages``.

The rest cover the pieces that make the property possible, and the two places
where in-memory state would otherwise survive a rollback and describe a database
that no longer exists.

What is *not* here is crash atomicity. A rollback in this process is always
correct; a power cut mid-transaction is not, because the undo log is in memory
and the buffer pool may already have written uncommitted pages out.
``tests/recovery/test_crash_and_corruption.py`` pins that boundary down in both
directions, and closing it is Milestone 9's job.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from engine import Column, Database, DataType, Schema
from engine.errors import BindingError, TransactionError, UniqueViolation
from engine.executor.engine import execute_script
from engine.storage.meta import META_HEADER_SIZE
from engine.transaction.manager import TransactionManager, TransactionState
from engine.transaction.undo import MAX_UNDO_BYTES, UndoLog

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("label", DataType.TEXT, nullable=False),
)

PAGE_SIZE = 512

#: Where the meta page keeps its LSN. Its checksum is always the last four
#: bytes of the header, so that one is derived from META_HEADER_SIZE.
_META_LSN = 60


@pytest.fixture
def db(tmp_path: Path):
    with Database.open(tmp_path / "txn.chendb", page_size=PAGE_SIZE) as handle:
        handle.create_table("t", SCHEMA)
        handle.insert_many("t", [(n, f"row{n:04d}") for n in range(40)])
        handle.sync()
        yield handle


def digest(db: Database) -> str:
    """A hash of every page the meta page claims, ignoring LSNs.

    Two things are deliberately excluded, and each is a thing rollback does not
    and cannot put back.

    **Trailing pages.** The hash covers the *referenced prefix*, not the whole
    file. A transaction that extended the file leaves those pages physically
    there; restoring the meta page takes ``page_count`` back, so nothing
    references them and the next allocation reuses their ids, but the bytes
    remain. ``test_a_rollback_may_leave_trailing_pages`` asserts that directly,
    so this helper is not quietly hiding it.

    **The LSN and the checksum that covers it.** Since Milestone 9 every page
    carries the LSN of the log record that last changed it, and a rollback *is*
    a change — the restore has to be logged, or recovery could not tell whether
    it reached the disk. So a rolled-back page comes back with the same contents
    and a higher LSN, and demanding byte-for-byte equality would be demanding
    that the rollback be unrecoverable.
    ``test_a_rollback_moves_the_lsn_forward`` asserts the LSN really does move,
    so that exclusion is not hiding anything either.
    """
    db.sync()
    raw = db.path.read_bytes()[: db.page_count * db.page_size]
    return hashlib.sha256(
        b"".join(
            _without_lsn(raw[n * db.page_size : (n + 1) * db.page_size], n)
            for n in range(db.page_count)
        )
    ).hexdigest()


def _without_lsn(page: bytes, page_id: int) -> bytes:
    """One page with its LSN and checksum blanked.

    The two layouts differ, and the offsets are read from the modules that own
    them rather than written out here — Milestone 9 put the LSN at 60 in the
    meta page and Milestone 10 pushed the checksum from 80 to 84, and a
    hardcoded copy silently blanked the wrong four bytes until a rollback test
    started failing for a reason that had nothing to do with rollback.
    """
    buf = bytearray(page)
    if page_id == 0:
        buf[_META_LSN : _META_LSN + 8] = bytes(8)
        buf[META_HEADER_SIZE - 4 : META_HEADER_SIZE] = bytes(4)
    else:
        # checksum u32 then lsn u64, at the very front.
        buf[0:12] = bytes(12)
    return bytes(buf)


# -- the undo log -----------------------------------------------------------


def test_the_first_image_of_a_page_is_the_one_kept():
    # First-write-wins is what bounds the log at pages-touched rather than
    # writes: restoring the earliest image undoes every later change too.
    log = UndoLog()
    assert log.capture(1, b"original") is True
    assert log.capture(1, b"later") is False
    assert [record.before_image for record in log.records()] == [b"original"]


def test_the_log_replays_newest_first():
    log = UndoLog()
    for page_id in (1, 2, 3):
        log.capture(page_id, bytes([page_id]))
    assert [record.page_id for record in log.rewind()] == [3, 2, 1]


def test_the_log_reports_what_it_is_holding():
    log = UndoLog()
    log.capture(1, b"x" * 100)
    log.capture(2, b"y" * 100)
    assert log.page_count == 2
    assert log.bytes_held == 200
    assert log.has(1) and not log.has(9)


def test_an_undo_log_that_outgrows_memory_stops_caching():
    """The ceiling bounds memory, not transaction size.

    Before Milestone 9 this raised: the in-memory log was the only copy, so
    running out of room meant the transaction could not be rolled back. The WAL
    now holds the same before-images on disk under the same first-write-wins
    rule, so overflowing is a cache miss rather than a failure — the log flags
    itself and :meth:`Database.rollback` reads what it needs from the WAL.
    """
    log = UndoLog()
    for page_id in range(MAX_UNDO_BYTES // 4096 + 2):
        log.capture(page_id, bytes(4096))

    assert log.overflowed
    assert log.bytes_held <= MAX_UNDO_BYTES
    assert log.page_count < MAX_UNDO_BYTES // 4096 + 2


def test_a_rollback_past_the_cap_still_restores_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The fallback, end to end, with the ceiling lowered so it is reachable.

    64 MiB of pages would take a while to write; 8 KiB does not, and exercises
    exactly the same path — the in-memory log gives up after two pages and the
    rest of the rollback comes off the disk.
    """
    monkeypatch.setattr("engine.transaction.undo.MAX_UNDO_BYTES", 2 * 512)

    with Database.open(tmp_path / "spill.chendb", page_size=512) as db:
        db.create_table("t", SCHEMA)
        db.insert_many("t", [(n, f"row{n:04d}") for n in range(200)])
        db.sync()
        before = digest(db)

        db.begin()
        db.insert_many("t", [(1000 + n, f"new{n:04d}") for n in range(200)])
        assert db.transactions.active.undo.overflowed, (
            "the point of the test is that memory ran out"
        )
        db.rollback()

        assert db.count("t") == 200
        assert digest(db) == before, "the WAL had what memory dropped"


def test_clearing_releases_everything():
    log = UndoLog()
    log.capture(1, b"x")
    log.clear()
    assert log.page_count == 0 and log.bytes_held == 0 and not log.has(1)


# -- the manager, against a dictionary --------------------------------------


def test_rollback_applies_every_before_image():
    manager = TransactionManager()
    disk = {1: b"new", 2: b"new"}
    manager.begin()
    manager.before_write(1, lambda: b"old-1")
    manager.before_write(2, lambda: b"old-2")
    manager.rollback(lambda page_id, image: disk.__setitem__(page_id, image))
    assert disk == {1: b"old-1", 2: b"old-2"}


def test_commit_applies_nothing():
    manager = TransactionManager()
    applied: list[int] = []
    manager.begin()
    manager.before_write(1, lambda: b"old")
    manager.commit()
    assert applied == []


def test_a_finished_transaction_releases_its_undo_log():
    manager = TransactionManager()
    manager.begin()
    manager.before_write(1, lambda: b"x" * 4096)
    transaction = manager.commit()
    assert transaction.undo_bytes == 0, "holding 4 KiB per committed page would leak"


def test_commit_with_nothing_open_is_an_error():
    with pytest.raises(TransactionError, match="COMMIT with no transaction"):
        TransactionManager().commit()


def test_transactions_do_not_nest():
    manager = TransactionManager()
    manager.begin()
    with pytest.raises(TransactionError, match="do not nest"):
        manager.begin()


def test_begin_adopts_an_implicit_transaction():
    # So a script reading `BEGIN; …; COMMIT;` behaves the way it looks, rather
    # than failing because the script already opened one.
    manager = TransactionManager()
    implicit = manager.begin(implicit=True)
    explicit = manager.begin()
    assert explicit is implicit
    assert not explicit.implicit


def test_an_implicit_begin_inside_a_transaction_is_a_no_op():
    manager = TransactionManager()
    outer = manager.begin()
    assert manager.begin(implicit=True) is outer


def test_the_history_is_bounded():
    manager = TransactionManager()
    for _ in range(TransactionManager.HISTORY_LIMIT + 20):
        manager.begin()
        manager.commit()
    assert len(manager.history()) == TransactionManager.HISTORY_LIMIT


def test_a_capture_needs_the_page_only_once():
    # before_write takes a callable so a page is read only when a record will
    # actually be kept — which, with first-write-wins, is the minority of writes.
    manager = TransactionManager()
    reads = 0

    def current() -> bytes:
        nonlocal reads
        reads += 1
        return b"x"

    manager.begin()
    for _ in range(10):
        manager.before_write(1, current)
    assert reads == 1


def test_capture_outside_a_transaction_does_nothing():
    manager = TransactionManager()
    manager.before_write(1, lambda: pytest.fail("must not read the page"))


# -- rollback through a real database ---------------------------------------


def test_a_rolled_back_insert_leaves_the_file_unchanged(db: Database):
    before = digest(db)
    db.begin()
    db.insert_many("t", [(1000 + n, f"new{n}") for n in range(100)])
    db.rollback()
    assert digest(db) == before, "byte-for-byte, not just row-for-row"


def test_a_rolled_back_delete_leaves_the_file_unchanged(db: Database):
    before = digest(db)
    victims = [rid for rid, _ in db.scan("t")][:10]
    db.begin()
    for rid in victims:
        db.delete("t", rid)
    db.rollback()
    assert digest(db) == before


def test_a_rolled_back_create_table_leaves_the_file_unchanged(db: Database):
    # The case Milestone 4's docstring called out as not atomic: several rows
    # across two system tables plus a fresh heap page. A physical undo fixes it
    # without the catalog knowing transactions exist.
    before = digest(db)
    db.begin()
    db.create_table("gone", Schema.of(Column("a", DataType.INTEGER)))
    assert "gone" in db.table_names()
    db.rollback()
    assert "gone" not in db.table_names()
    assert digest(db) == before


def test_a_rolled_back_create_index_leaves_the_file_unchanged(db: Database):
    before = digest(db)
    db.begin()
    db.create_index("t_label", "t", "label")
    assert [index.name for index in db.indexes()] == ["t_label"]
    db.rollback()
    assert db.indexes() == []
    assert digest(db) == before


def test_a_committed_change_survives_a_reopen(db: Database):
    path = db.path
    db.begin()
    db.insert("t", (9999, "kept"))
    db.commit()
    db.close()
    with Database.open(path, page_size=PAGE_SIZE) as reopened:
        assert reopened.count("t") == 41


def test_a_rolled_back_change_is_absent_after_a_reopen(db: Database):
    path = db.path
    db.begin()
    db.insert("t", (9999, "gone"))
    db.rollback()
    db.close()
    with Database.open(path, page_size=PAGE_SIZE) as reopened:
        assert reopened.count("t") == 40


def test_rollback_survives_the_pages_having_been_evicted(tmp_path: Path):
    # The buffer pool may steal a dirty uncommitted page and write it to disk.
    # Rollback still works: the before-images are in memory, and writing one
    # back re-admits the page whether it was resident or not.
    path = tmp_path / "steal.chendb"
    with Database.open(path, page_size=PAGE_SIZE, buffer_pool_frames=4) as db:
        db.create_table("t", SCHEMA)
        db.insert_many("t", [(n, f"row{n:04d}") for n in range(40)])
        db.sync()
        before = digest(db)

        db.begin()
        db.insert_many("t", [(1000 + n, f"new{n}") for n in range(200)])
        assert db.pager.stats.physical_writes > 0, "the pool must have stolen"
        db.rollback()
        assert digest(db) == before


def test_rolling_back_an_allocation_restores_the_page_count(db: Database):
    # The meta page is a decoded dataclass, so restoring its bytes is not
    # enough — page_count and next_object_id have to be re-read.
    before = db.page_count
    db.begin()
    db.insert_many("t", [(2000 + n, f"grow{n}") for n in range(200)])
    assert db.page_count > before
    db.rollback()
    assert db.page_count == before


def test_a_rollback_may_leave_trailing_pages(db: Database):
    # The one thing rollback does not undo. Restoring the meta page takes
    # page_count back, so nothing references them; the file is simply longer
    # than it needs to be until the next allocation reuses the ids.
    db.sync()
    size_before = db.path.stat().st_size
    pages_before = db.page_count

    db.begin()
    db.insert_many("t", [(8000 + n, f"grow{n}") for n in range(200)])
    db.rollback()
    db.sync()

    assert db.page_count == pages_before, "nothing references the extra pages"
    assert db.path.stat().st_size > size_before, "but they are still in the file"


def test_a_file_longer_than_its_meta_page_still_opens(db: Database):
    # A rolled-back allocation leaves trailing pages nothing references — the
    # same state a crash between extending the file and updating the meta page
    # leaves. Refusing to open it would make rollback unable to shrink.
    path = db.path
    db.begin()
    db.insert_many("t", [(3000 + n, f"grow{n}") for n in range(200)])
    db.rollback()
    db.close()
    with Database.open(path, page_size=PAGE_SIZE) as reopened:
        assert reopened.count("t") == 40


def test_the_catalog_cache_does_not_survive_a_rollback(db: Database):
    # Otherwise the engine keeps serving a table whose rows are no longer there.
    db.begin()
    db.create_table("cached", Schema.of(Column("a", DataType.INTEGER)))
    db.insert("cached", (1,))
    db.rollback()
    assert db.table("cached") is None


def test_the_statistics_do_not_survive_a_rollback(db: Database):
    db.analyze("t")
    db.begin()
    db.insert_many("t", [(4000 + n, f"x{n}") for n in range(50)])
    db.analyze("t")
    assert db.statistics.for_table("t").row_count == 90
    db.rollback()
    assert db.statistics.for_table("t").row_count == 40


def test_indexes_are_rolled_back_with_the_rows(db: Database):
    # A logical undo would have to know to remove the index entry too. A
    # physical one restores the index pages because they are just pages.
    db.create_index("t_label", "t", "label")
    db.begin()
    db.insert("t", (7777, "findme"))
    assert db.lookup("t_label", "findme")
    db.rollback()
    assert db.lookup("t_label", "findme") == []
    db.tree_for("t_label").verify()


def test_a_rollback_moves_the_lsn_forward(db: Database):
    """The restore is itself a logged change, and the page says so.

    This is what :func:`digest` excludes, asserted directly. A rolled-back page
    holds the same data at a higher LSN — because if it came back with its old
    LSN, recovery would compare the log record for the restore against it, find
    the page already ahead, and skip putting it back.
    """
    transaction = db.begin()
    db.insert("t", (950, "a"))
    # Ask the transaction which page it touched rather than guessing: the row
    # lands on whichever heap page had room, not necessarily the first. Page 0
    # is skipped because the meta page has its own layout and its own reader.
    page_id = next(r.page_id for r in transaction.records() if r.page_id != 0)
    before = db.pager.read_page(page_id).lsn
    db.rollback()

    after = db.pager.read_page(page_id).lsn
    assert after > before, "the restore is a change, and carries its own LSN"


# -- the context manager ----------------------------------------------------


def test_the_context_manager_commits_on_success(db: Database):
    with db.transaction():
        db.insert("t", (5555, "ok"))
    assert db.count("t") == 41
    assert not db.in_transaction


def test_the_context_manager_rolls_back_on_an_exception(db: Database):
    before = digest(db)
    with pytest.raises(ValueError), db.transaction():
        db.insert("t", (5555, "doomed"))
        raise ValueError("something went wrong")
    assert digest(db) == before
    assert not db.in_transaction


def test_the_context_manager_adopts_an_outer_transaction(db: Database):
    # No savepoints, so an inner block cannot roll back on its own. Adopting is
    # honest; pretending to nest would not be.
    db.begin()
    with db.transaction():
        db.insert("t", (6666, "inner"))
    assert db.in_transaction, "the outer transaction is still open"
    db.rollback()
    assert db.count("t") == 40


def test_the_context_manager_tolerates_a_block_that_ends_it(db: Database):
    """``db.rollback()`` inside a ``with`` is reasonable to write.

    The context manager used to commit over the top of it and raise "COMMIT with
    no transaction open", which hides whatever the block was actually doing.
    """
    before = digest(db)
    with db.transaction():
        db.insert("t", (900, "a"))
        db.rollback()
    assert not db.in_transaction
    assert digest(db) == before


def test_the_context_manager_tolerates_a_block_that_commits(db: Database):
    with db.transaction():
        db.insert("t", (901, "a"))
        db.commit()
    assert not db.in_transaction
    assert db.count("t") == 41


# -- through SQL ------------------------------------------------------------


def test_explicit_rollback_through_sql(db: Database):
    before = digest(db)
    execute_script("BEGIN; INSERT INTO t VALUES (100, 'a'), (101, 'b'); ROLLBACK;", db)
    assert digest(db) == before


def test_explicit_commit_through_sql(db: Database):
    execute_script("BEGIN; INSERT INTO t VALUES (100, 'a'); COMMIT;", db)
    assert db.count("t") == 41


def test_a_script_that_fails_half_way_applies_none_of_it(db: Database):
    # The promise execute_script's docstring has made since Milestone 3.
    before = digest(db)
    with pytest.raises(BindingError):
        execute_script("INSERT INTO t VALUES (200, 'a'); INSERT INTO nope VALUES (1);", db)
    assert digest(db) == before


def test_a_single_statement_is_atomic_on_its_own(db: Database):
    # A multi-row INSERT that fails part-way used to leave the earlier rows.
    db.create_index("t_id", "t", "id", unique=True)
    before = digest(db)
    with pytest.raises(UniqueViolation):
        execute_script("INSERT INTO t VALUES (500, 'a'), (501, 'b'), (0, 'dup');", db)
    assert digest(db) == before


def test_a_script_leaves_no_transaction_open(db: Database):
    execute_script("INSERT INTO t VALUES (300, 'a');", db)
    assert not db.in_transaction


def test_a_failing_script_leaves_no_transaction_open(db: Database):
    with pytest.raises(BindingError):
        execute_script("INSERT INTO nope VALUES (1);", db)
    assert not db.in_transaction


def test_a_commit_mid_script_ends_that_transaction(db: Database):
    execute_script(
        "BEGIN; INSERT INTO t VALUES (400, 'a'); COMMIT; INSERT INTO t VALUES (401, 'b');",
        db,
    )
    assert db.count("t") == 42
    assert not db.in_transaction


def test_a_lone_begin_leaves_the_transaction_open(db: Database):
    """The script's auto-commit must not swallow the client's own BEGIN.

    Sending ``BEGIN;`` and nothing else is how the explorer's SQL console opens
    a transaction it intends to close in a *later* request. If ``execute_script``
    committed whatever was open at the end, that would be a silent no-op — the
    user would type BEGIN, see success, and have no transaction.
    """
    execute_script("BEGIN;", db)
    assert db.in_transaction
    assert db.transactions.in_explicit_transaction

    execute_script("INSERT INTO t VALUES (700, 'a');", db)
    assert db.in_transaction, "the next script joins the open transaction"

    db.rollback()
    assert db.count("t") == 40


# -- a failed statement dooms the transaction -------------------------------
#
# PostgreSQL's rule, because the alternative is worse. A client that opened the
# transaction in an earlier request owns it, so `execute_script` will not unwind
# it — which used to mean an error left the partial work in place and a later
# COMMIT kept it. Half a transaction, committed, is the one outcome this
# milestone exists to prevent.


def test_a_failed_statement_marks_an_explicit_transaction_failed(db: Database):
    execute_script("BEGIN;", db)
    with pytest.raises(BindingError):
        execute_script("INSERT INTO t VALUES (701, 'a'); INSERT INTO nope VALUES (1);", db)

    assert db.in_transaction, "still open — the client owns it"
    assert db.transactions.is_failed
    assert db.transactions.active is not None
    assert db.transactions.active.state is TransactionState.FAILED


def test_a_failed_transaction_refuses_further_statements(db: Database):
    execute_script("BEGIN;", db)
    with pytest.raises(BindingError):
        execute_script("INSERT INTO nope VALUES (1);", db)

    with pytest.raises(BindingError, match="current transaction is aborted"):
        execute_script("INSERT INTO t VALUES (702, 'a');", db)
    with pytest.raises(BindingError, match="current transaction is aborted"):
        execute_script("SELECT * FROM t;", db)


def test_committing_a_failed_transaction_rolls_it_back(db: Database):
    before = digest(db)
    execute_script("BEGIN;", db)
    execute_script("INSERT INTO t VALUES (703, 'a');", db)
    with pytest.raises(BindingError):
        execute_script("INSERT INTO nope VALUES (1);", db)

    (result,) = execute_script("COMMIT;", db)
    assert "rolled back" in result.message
    assert not db.in_transaction
    assert digest(db) == before, "nothing from the doomed transaction survived"


def test_rollback_still_works_on_a_failed_transaction(db: Database):
    before = digest(db)
    execute_script("BEGIN;", db)
    execute_script("INSERT INTO t VALUES (704, 'a');", db)
    with pytest.raises(BindingError):
        execute_script("INSERT INTO nope VALUES (1);", db)

    execute_script("ROLLBACK;", db)
    assert digest(db) == before


def test_the_manager_alone_refuses_to_commit_a_failed_transaction(db: Database):
    # Going around Database.commit() must not be a way to keep the work.
    db.begin()
    db.insert("t", (705, "a"))
    db.transactions.mark_failed()
    with pytest.raises(TransactionError, match="can only be rolled back"):
        db.transactions.commit()


def test_an_implicit_transaction_is_unwound_not_left_failed(db: Database):
    # Nobody owns it, so leaving it open would strand the next statement.
    with pytest.raises(BindingError):
        execute_script("INSERT INTO nope VALUES (1);", db)
    assert not db.in_transaction
    assert not db.transactions.is_failed


def test_commit_with_nothing_open_is_reported_with_a_position(db: Database):
    with pytest.raises(BindingError, match="COMMIT with no transaction"):
        execute_script("COMMIT;", db, atomic=False)


def test_atomic_false_runs_statements_independently(db: Database):
    # The escape hatch for a caller that wants per-statement autocommit, which
    # is what the SQL standard actually specifies.
    with pytest.raises(BindingError):
        execute_script(
            "INSERT INTO t VALUES (600, 'a'); INSERT INTO nope VALUES (1);",
            db,
            atomic=False,
        )
    assert db.count("t") == 41, "the first statement stood on its own"


# -- reporting --------------------------------------------------------------


def test_a_transaction_reports_what_it_did(db: Database):
    db.begin()
    db.insert_many("t", [(700 + n, f"x{n}") for n in range(20)])
    transaction = db.transactions.active
    assert transaction.state is TransactionState.ACTIVE
    assert transaction.pages_held > 0
    assert transaction.undo_bytes == transaction.pages_held * PAGE_SIZE
    assert transaction.pages_written >= transaction.pages_held
    db.rollback()
    assert transaction.state is TransactionState.ABORTED
    assert transaction.pages_restored > 0


def test_finished_transactions_are_kept_for_the_timeline(db: Database):
    db.begin()
    db.commit()
    db.begin()
    db.rollback()
    history = db.transactions.history()
    assert [t.state for t in history[-2:]] == [
        TransactionState.COMMITTED,
        TransactionState.ABORTED,
    ]


def test_transaction_events_are_emitted(db: Database):
    from engine.diagnostics import RingBufferSink, TraceLevel, Tracer

    sink = RingBufferSink(capacity=10_000)
    tracer = Tracer(sink, TraceLevel.STORAGE)
    db._tracer = tracer
    db._transactions._tracer = tracer

    db.begin()
    db.insert("t", (800, "x"))
    db.rollback()

    actions = [
        item.event.action
        for item in sink.snapshot()
        if item.event_type == "TransactionEvent"
    ]
    assert actions == ["begin", "rollback_started", "rollback_done"]
    assert any(item.event_type == "UndoRecordEvent" for item in sink.snapshot())
