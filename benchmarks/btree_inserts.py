#!/usr/bin/env python3
"""Measure what a B+ tree costs to build, and to keep.

    python benchmarks/btree_inserts.py

`benchmarks/index_vs_scan.py` prices an index once it exists. This prices the
other half, which is the half a `CREATE INDEX` on a large table notices:

  * **building**, row by row, at O(n log n) with a split every half node;
  * **maintaining**, because an insert into an indexed table is a heap append
    *and* a descent plus a leaf write per index;
  * **shape**, because height is what a lookup pays and it grows in steps, not
    smoothly.

ChenDB does not bulk load: a real system sorts the keys first and packs leaves to
capacity in one pass, which is O(n) after the sort and leaves no half-empty
nodes. The occupancy column below is what that omission costs, and it is the
number to watch if bulk loading ever lands.

Absolute times depend on the machine. The per-row costs and the occupancy do
not.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema

PAGE_SIZE = 4096
SIZES = (1_000, 5_000, 20_000)
LOOKUPS = 200

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("email", DataType.TEXT, nullable=False),
    Column("bucket", DataType.INTEGER, nullable=False),
)


def rows(count: int) -> list[tuple[int, str, int]]:
    return [(n, f"user{n:06d}@example.com", n % 1000) for n in range(count)]


def populated(path: Path, count: int) -> Database:
    """A table of ``count`` rows and no secondary index."""
    db = Database.open(path, page_size=PAGE_SIZE)
    db.create_table("users", SCHEMA)
    db.insert_many("users", rows(count))
    db.sync()
    return db


def header(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def main() -> int:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        print(
            f"ChenDB B+ tree benchmark - {PAGE_SIZE}-byte pages\n"
            "One index per size, built over an existing table."
        )

        header("Build cost against size")
        print(
            f"  {'rows':>8}{'build':>11}{'us/row':>9}{'height':>8}"
            f"{'splits':>8}{'pages':>7}{'bytes/entry':>13}"
        )
        for count in SIZES:
            db = populated(root / f"build{count}.chendb", count)
            started = time.perf_counter_ns()
            db.create_index("users_bucket", "users", "bucket")
            elapsed = (time.perf_counter_ns() - started) / 1e6
            tree = db.tree_for("users_bucket")
            pages = len(tree.page_ids())
            entries = tree.count()
            print(
                f"  {count:>8,}{elapsed:>8.1f} ms{elapsed * 1000 / count:>9.1f}"
                f"{tree.height:>8}{tree.stats.splits:>8}{pages:>7}"
                f"{pages * PAGE_SIZE / max(entries, 1):>13.0f}"
            )
            db.close()
        print(
            "\n  An entry is a key and a record id, well under twenty bytes, so a\n"
            "  bytes-per-entry figure several times that is the space a row-by-row\n"
            "  build leaves behind: a leaf splits in half and both halves stay\n"
            "  half empty until later inserts land in them. Sorting first and\n"
            "  packing leaves would remove it, and would also make the build one\n"
            "  pass instead of n descents."
        )

        header("What an index costs on every insert")
        print(f"  {'rows added':>11}{'no index':>12}{'one index':>12}{'two':>12}")
        for count in (200, 1_000):
            timings = []
            for indexes in (0, 1, 2):
                db = populated(root / f"m{count}{indexes}.chendb", 500)
                if indexes >= 1:
                    db.create_index("users_bucket", "users", "bucket")
                if indexes >= 2:
                    db.create_index("users_email", "users", "email", unique=True)
                extra = [
                    (500 + n, f"extra{n:06d}@example.com", n % 1000) for n in range(count)
                ]
                started = time.perf_counter_ns()
                for row in extra:
                    db.insert("users", row)
                timings.append((time.perf_counter_ns() - started) / 1e6)
                db.close()
            print(
                f"  {count:>11,}{timings[0]:>9.1f} ms{timings[1]:>9.1f} ms"
                f"{timings[2]:>9.1f} ms"
            )
        print(
            "\n  Every index is a second structure to keep true, and the primary\n"
            "  key is already one of them: the 'no index' column still maintains\n"
            "  users_pkey, because a PRIMARY KEY here is a real unique index and\n"
            "  not a note in the catalog. This is the cost that makes an unused\n"
            "  index worse than no index."
        )

        header("Height, and what a lookup pays for it")
        print(
            f"  {'rows':>8}{'height':>8}{'pages/lookup':>14}"
            f"{'us/lookup':>12}{'ordered scan':>15}"
        )
        for count in SIZES:
            db = populated(root / f"look{count}.chendb", count)
            db.create_index("users_bucket", "users", "bucket")
            tree = db.tree_for("users_bucket")
            samples = []
            reads_before = db.stats.page_reads
            for n in range(LOOKUPS):
                started = time.perf_counter_ns()
                db.lookup("users_bucket", n % 1000)
                samples.append((time.perf_counter_ns() - started) / 1000)
            pages = (db.stats.page_reads - reads_before) / LOOKUPS
            started = time.perf_counter_ns()
            walked = sum(1 for _ in tree.range_scan(None, None))
            ordered = (time.perf_counter_ns() - started) / 1e6
            print(
                f"  {count:>8,}{tree.height:>8}{pages:>14.1f}"
                f"{statistics.median(samples):>12.1f}{ordered:>12.1f} ms"
            )
            assert walked == count, "the ordered scan lost entries"
            db.close()
        print(
            "\n  A lookup on `bucket` matches many rows (1,000 distinct values over\n"
            "  the whole table) so pages per lookup is the descent plus one heap\n"
            "  read per match, which is why it grows with the table while the\n"
            "  height barely moves. The ordered scan walks the linked leaves and\n"
            "  returns every entry in key order with no sort at all."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
