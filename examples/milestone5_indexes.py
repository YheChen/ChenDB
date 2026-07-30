#!/usr/bin/env python3
"""A narrated tour of the Milestone 5 B+ tree.

    python examples/milestone5_indexes.py

Six things, in the order they matter: why the record encoding could not be
reused for keys, what the tree looks like on disk, what a lookup costs, what an
index costs to maintain, when the planner picks it, and when picking it is a
mistake.
"""

from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.executor import execute_script
from engine.executor.operators import IndexScan, SeqScan
from engine.index.key import decode_key, describe_key, encode_key
from engine.index.node import BTreeNode
from engine.storage.constants import INVALID_PAGE_ID

USERS = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("email", DataType.TEXT, nullable=False),
    Column("age", DataType.INTEGER),
)

ROW_COUNT = 400
PAGE_SIZE = 512


def rule(title: str) -> None:
    print(f"\n{'─' * 78}\n{title}\n")


def access_path(plan) -> str:
    stack, found = [plan], "?"
    while stack:
        node = stack.pop()
        if isinstance(node, IndexScan):
            found = f"IndexScan  {node.condition}"
        elif isinstance(node, SeqScan):
            found = "SeqScan"
        stack.extend(node.children)
    return found


def main() -> int:
    print("ChenDB Milestone 5, B+ tree indexes")

    # -- 1. why a separate key encoding -----------------------------------
    rule("1. Little-endian records cannot be compared as bytes")

    print("   The record encoding writes integers little-endian, because that")
    print("   matches every CPU and makes writing one a memory copy:\n")
    for value in (1, 256, -1):
        record = struct.pack("<q", value)
        key = encode_key(value, DataType.INTEGER)
        print(f"     {value:>5}   record {record.hex(' ')}   key {key.hex(' ')}")

    print()
    print(
        f"   record bytes:  1 > 256 is {struct.pack('<q', 1) > struct.pack('<q', 256)}"
        "   <- the first byte read is the least significant one"
    )
    print(
        f"   index keys:    1 > 256 is "
        f"{encode_key(1, DataType.INTEGER) > encode_key(256, DataType.INTEGER)}"
        "  <- big-endian, with the sign bit flipped"
    )
    print("\n   So a comparison is one memcmp, and the tree never learns what")
    print("   type a key holds. RocksDB and FoundationDB do the same.")

    print("\n   The tag byte orders NULL against everything else:")
    for label, key in (
        ("NULL", encode_key(None, DataType.INTEGER)),
        ("-1", encode_key(-1, DataType.INTEGER)),
        ("'ada'", encode_key("ada", DataType.TEXT)),
    ):
        print(f"     {label:<7} {key.hex(' ')}")
    print("   NULL sorts below every value, which is why `WHERE age < 18` has to")
    print("   anchor its lower bound above the NULL tag. No comparison is ever")
    print("   true for NULL.")

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "shop.chendb"
        sink = RingBufferSink(capacity=200_000)
        tracer = Tracer(sink, TraceLevel.STORAGE)

        with Database.open(path, page_size=PAGE_SIZE, tracer=tracer) as db:
            db.create_table("users", USERS)
            db.insert_many(
                "users",
                [(n, f"u{n:04d}@example.com", 18 + n % 45) for n in range(ROW_COUNT)],
            )

            # -- 2. the tree on disk ---------------------------------------
            rule("2. What CREATE INDEX builds")

            sink.clear()
            index = db.create_index("users_age", "users", "age")
            tree = db.tree_for("users_age")
            print(
                f"   {ROW_COUNT} rows -> height {tree.height}, "
                f"{len(tree.page_ids())} pages, {tree.stats.splits} splits "
                f"({tree.stats.root_splits} of them root splits)"
            )
            print(f"   catalog row: chendb_indexes -> root_page {index.root_page}")

            root = BTreeNode(db.read_page(tree.root_page_id))
            print(f"\n   root page {root.page_id} ({root.page.page_type.name}):")
            for entry in root.entries():
                print(
                    f"     {describe_key(entry.key, DataType.INTEGER):>6}"
                    f"  -> child page {entry.child_page_id}"
                )
            print("   Slot 0 is -∞: that sentinel is why an internal node needs no")
            print("   special 'leftmost child' field, and why a descent always finds")
            print("   a child to follow.")

            leaf = BTreeNode(db.read_page(root.entry_at(0).child_page_id))
            while not leaf.is_leaf:
                leaf = BTreeNode(db.read_page(leaf.entry_at(0).child_page_id))
            entries = leaf.entries()
            print(f"\n   leftmost leaf, page {leaf.page_id} ({leaf.count} entries):")
            for entry in entries[:4]:
                print(
                    f"     key {describe_key(entry.key, DataType.INTEGER):>4}"
                    f"  -> row at {entry.record_id}"
                )
            print(f"     ... {leaf.count - 4} more")
            print(f"   next leaf: page {leaf.next_leaf_id}")

            chain, page_id, guard = 0, leaf.page_id, 0
            while page_id != INVALID_PAGE_ID and guard < 10_000:
                chain += 1
                guard += 1
                page_id = BTreeNode(db.read_page(page_id)).next_leaf_id
            print(f"   the chain reaches {chain} leaves, one descent, then sideways.")

            splits = [
                item.event
                for item in sink.snapshot()
                if item.event_type == "NodeSplitEvent"
            ]
            root_splits = [event for event in splits if event.is_root_split]
            print(
                f"\n   {len(splits)} NodeSplitEvents, {len(root_splits)} of them root splits."
            )
            print("   A root split is the only thing that changes the tree's height:")
            for event in root_splits:
                print(
                    f"     page {event.page_id} -> {event.new_page_id}, "
                    f"promoted {event.promoted_key}, level {event.tree_level}"
                )

            # -- 3. what a lookup costs ------------------------------------
            rule("3. What a lookup costs")

            key = encode_key(30, DataType.INTEGER)
            before = db.stats.page_reads
            matches = tree.search(key)
            print(
                f"   search(age = 30)  ->  {len(matches)} match(es), "
                f"{db.stats.page_reads - before} pages read, "
                f"tree height {tree.height}"
            )
            print(f"   path: {' -> '.join(f'p{p}' for p in tree.descent_path(key))}")
            print(
                f"\n   The table itself is {db.count('users')} rows over "
                f"{len(list(db.heap_for('users').page_ids()))} heap pages."
            )
            print("   A sequential scan reads all of them. The index reads the height.")

            low, high = (
                encode_key(30, DataType.INTEGER),
                encode_key(33, DataType.INTEGER),
            )
            found = [decode_key(k, DataType.INTEGER) for k, _ in tree.range_scan(low, high)]
            print(
                f"\n   range 30..33  ->  {len(found)} entries, "
                f"in key order, no sort: {sorted(set(found))}"
            )

            # -- 4. maintenance --------------------------------------------
            rule("4. What an index costs to maintain")

            db.create_index("users_email", "users", "email", unique=True)
            print(f"   indexes on users: {[i.name for i in db.indexes('users')]}")

            before = db.stats.page_writes
            db.insert("users", (9999, "late@example.com", 30))
            print(
                f"\n   one INSERT with two indexes: "
                f"{db.stats.page_writes - before} page writes"
            )
            print("   (the heap write, plus a descent and a write per index)")
            print("   the new row is findable through both:")
            print(f"     by age    {len(db.lookup('users_age', 30))} rows")
            print(f"     by email  {db.lookup('users_email', 'late@example.com')}")

            record_id = next(r for r, row in db.scan("users") if row[0] == 9999)
            db.delete("users", record_id)
            print(
                f"\n   after DELETE, the index entry is gone too: "
                f"{db.lookup('users_email', 'late@example.com')}"
            )
            print("   The row had to be *read* first. An index entry is keyed on the")
            print("   value, so removing it needs to know what the value was. This is")
            print("   why PostgreSQL leaves dead entries for VACUUM instead.")

            try:
                db.create_index("bad", "users", "age", unique=True)
            except Exception as exc:
                print("\n   CREATE UNIQUE INDEX on a column with duplicates:")
                print(f"     {type(exc).__name__}: {exc}")

            # -- 5. the planner --------------------------------------------
            rule("5. When the planner reaches for the index")

            for sql in (
                "SELECT id FROM users WHERE age = 30",
                "SELECT id FROM users WHERE age >= 30 AND age < 33",
                "SELECT id FROM users WHERE age < 20",
                "SELECT id FROM users WHERE age = 30 AND email = 'u0012@example.com'",
                "SELECT id FROM users WHERE age <> 30",
                "SELECT id FROM users WHERE id = 42",
            ):
                result = execute_script(sql, db)[0]
                print(f"   {sql}")
                print(
                    f"     {access_path(result.plan):<44}"
                    f"{result.stats.rows_returned:>4} rows, "
                    f"{result.stats.pages_read:>4} pages"
                )
            print("\n   `<>` is refused on purpose: an index cannot bound it, so the")
            print("   scan would read the whole tree and then fetch every row anyway.")
            print("   `id = 42` has no index and falls back to a scan.")

            # -- 6. when that is wrong -------------------------------------
            rule("6. When reaching for it is a mistake")

            wide = "SELECT id FROM users WHERE age >= 18"
            narrow = "SELECT id FROM users WHERE age = 30"
            for sql in (narrow, wide):
                result = execute_script(sql, db)[0]
                heap_pages = len(list(db.heap_for("users").page_ids()))
                print(
                    f"   {sql}\n"
                    f"     {result.stats.rows_returned:>4} rows   "
                    f"{result.stats.pages_read:>5} pages read   "
                    f"(a full scan would read {heap_pages})"
                )
            print("\n   The wide predicate matches nearly everything, so the index")
            print("   does one random heap read per row, through pages a sequential")
            print("   scan would have read once each. Milestone 5 chooses by rule and")
            print("   cannot tell; Milestone 6's cost model is what fixes it.")
            print("\n   benchmarks/index_vs_scan.py measures the crossover.")

        # -- persistence ---------------------------------------------------
        rule("7. Reopening the file")

        with Database.open(path, page_size=PAGE_SIZE) as db:
            print("   indexes recovered from chendb_indexes:")
            for info in db.indexes():
                tree = db.tree_for(info.name)
                tree.verify()
                print(
                    f"     {info.name:<14} on {info.table_name}.{info.column_name:<8}"
                    f"root p{info.root_page}  height {tree.height}  "
                    f"{tree.count()} entries  verified"
                )
            print("\n   The root page id is a catalog row, so a root split during a")
            print("   previous session still resolves. Miss that and the index would")
            print("   answer correctly until the database closed, then come back")
            print("   rooted at what is now an interior node.")

    print("\n" + "─" * 78)
    print("Try it in the browser: python -m engine.server, then the Indexes tab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
