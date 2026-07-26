#!/usr/bin/env python3
"""A narrated tour of the Milestone 1 storage engine.

    python examples/milestone1_storage.py

Creates a database in a temporary directory, fills it, kills the handle,
reopens it, and shows the bytes at each step. Everything printed is read back
from the real file — nothing is simulated.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.storage.inspect import hexdump, render_page_map

#: Small enough that four rows fill a page, so chaining shows up immediately.
PAGE_SIZE = 256

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("email", DataType.TEXT, nullable=False),
    Column("age", DataType.INTEGER),
    Column("active", DataType.BOOLEAN),
)

ROWS: list[tuple[object, ...]] = [
    (1, "ada@example.com", 36, True),
    (2, "alan@example.com", None, False),
    (3, "grace@example.com", 45, True),
    (4, "edgar@example.com", 51, True),
    (5, "jim@example.com", None, False),
    (6, "barbara@example.com", 62, True),
]


def heading(number: int, text: str) -> None:
    print(f"\n\033[1m{number}. {text}\033[0m")
    print("─" * 74)


def main() -> int:
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "demo.chendb"
        sink = RingBufferSink(capacity=10_000)
        tracer = Tracer(sink, TraceLevel.STORAGE)

        # ------------------------------------------------------------------
        heading(1, "Create the file")
        db = Database.open(path, page_size=PAGE_SIZE, tracer=tracer)
        print(f"   {path.name} is {path.stat().st_size} bytes: exactly one page.")
        print("   Page 0 is the meta page. Its first 16 bytes identify the file:")
        print()
        print(hexdump(path.read_bytes()[:32], limit=32))

        # ------------------------------------------------------------------
        heading(2, "Define a table")
        db.create_table("users", SCHEMA)
        print(f"   {len(SCHEMA)} columns, null bitmap {SCHEMA.null_bitmap_size} byte(s).")
        print(f"   Fixed row size: {SCHEMA.fixed_row_size or 'variable (TEXT column)'}")
        print(f"   The file now holds {db.page_count} pages: the meta page, the")
        print("   three catalog heaps, and this table's first heap page.")
        print("   Milestone 1 kept the schema in a JSON page; since Milestone 4 it")
        print("   is rows in chendb_tables and chendb_columns, so a reopened")
        print("   database rebuilds it from the catalog like any other table.")

        # ------------------------------------------------------------------
        heading(3, "Insert rows and watch the heap chain grow")
        for row in ROWS:
            record_id = db.insert("users", row)
            page_count = db.page_count
            print(f"   {row[1]!s:<22} → {record_id}   file: {page_count} pages")
        print()
        print(f"   {len(ROWS)} rows over {len(db.heap_page_ids("users"))} heap page(s).")
        print("   Pages are threaded by next_page_id; the meta page remembers")
        print("   the last one, so appending is O(1) instead of walking the chain.")

        # ------------------------------------------------------------------
        heading(4, "Look inside a heap page")
        heap_page_id = min(db.heap_page_ids("users"))
        detail = db.page_detail(heap_page_id)
        print(render_page_map(detail))
        print()
        for slot in detail.slots:
            if slot.record is None:
                continue
            fields = "  ".join(
                f"{field.name}={'NULL' if field.is_null else repr(field.value)}"
                for field in slot.record.fields
            )
            print(f"   slot {slot.slot_id}  @{slot.offset:<4} {slot.length:>3}B  {fields}")
        print()

        nullable_slot = next(
            s for s in detail.slots if s.record and any(f.is_null for f in s.record.fields)
        )
        record = nullable_slot.record
        assert record is not None
        bits = "".join(
            "1" if record.null_bitmap[i // 8] >> (i % 8) & 1 else "0"
            for i in range(len(record.fields))
        )
        null_names = [f.name for f in record.fields if f.is_null]
        print(f"   Row with a NULL: bitmap is 0x{record.null_bitmap.hex()} = {bits}")
        print(f"   → {', '.join(null_names)} is absent from the bytes entirely.")
        print()
        print("   The raw bytes of that record:")
        print(hexdump(bytes.fromhex(nullable_slot.raw_hex), start_offset=nullable_slot.offset))

        # ------------------------------------------------------------------
        heading(5, "Delete a row — a tombstone, not an erasure")
        victim = next(rid for rid, _ in db.scan("users"))
        db.delete("users", victim)
        after = db.page_detail(victim.page_id)
        print(f"   Deleted {victim}.")
        print(f"   Slot {victim.slot_id} is now a tombstone, but slot_count is still")
        print(f"   {after.summary.slot_count} — later slot ids must not renumber.")
        print(f"   Reclaimable space: {after.summary.reclaimable_space} bytes,")
        print("   recoverable by compaction, which preserves every slot id.")

        # ------------------------------------------------------------------
        heading(6, "Close the handle, then open the file again")
        first_handle_stats = db.stats.as_dict()
        db.close()
        print(f"   Closed. On disk: {path.stat().st_size} bytes.")

        reopened = Database.open(path, tracer=tracer)
        info = reopened.require_table("users")
        print(f"   Reopened. Table {info.name!r} came back from the file.")
        print(f"   Columns: {', '.join(info.schema.column_names)}")
        print()
        for record_id, row in reopened.scan("users"):
            rendered = ", ".join("NULL" if v is None else str(v) for v in row)
            print(f"   {record_id}  {rendered}")
        print()
        print(f"   {reopened.count('users')} rows survived the restart.")

        # ------------------------------------------------------------------
        heading(7, "The whole file, page by page")
        print(
            f"   {'id':>3}  {'type':<8} {'offset':>7} {'slots':>6} {'live':>5} "
            f"{'free':>6}  {'ck':<3} owner"
        )
        for summary in reopened.page_summaries():
            print(
                f"   {summary.page_id:>3}  {summary.page_type:<8} "
                f"{summary.file_offset:>7} {summary.slot_count:>6} "
                f"{summary.live_record_count:>5} {summary.free_space:>6}  "
                f"{'ok' if summary.checksum_valid else 'BAD':<3} {summary.owner}"
            )

        # ------------------------------------------------------------------
        heading(8, "What the engine reported while doing all that")
        # I/O counters live on the pager, so they reset when a handle is
        # reopened. Both halves of the session are shown separately.
        print("   pager counters      writing   reading")
        for key in ("page_reads", "page_writes", "allocations", "syncs"):
            before = first_handle_stats[key]
            after = getattr(reopened.stats, key)
            print(f"   {key:<18} {before:>7}   {after:>7}")
        print()
        counts: dict[str, int] = {}
        for item in sink.snapshot():
            counts[item.event_type] = counts.get(item.event_type, 0) + 1
        for event_type, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"   {event_type:<24} {count:>5}")
        print()
        print(f"   {sink.stats.total_recorded} events at TraceLevel.STORAGE.")
        print("   At TraceLevel.OFF the identical workload produces a")
        print("   byte-identical file and zero events.")

        reopened.close()

        print("\n" + "─" * 74)
        print("Every number above was read back from the file, not remembered.")
        print("Try it yourself:  python -m engine mydb.chendb")
        print("Or in the browser:  python -m engine.server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
