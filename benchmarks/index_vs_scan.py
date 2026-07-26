#!/usr/bin/env python3
"""Measure when a B+ tree index beats a sequential scan — and when it loses.

    python benchmarks/index_vs_scan.py

The whole argument for an index is that a point lookup costs O(log n) page
reads instead of O(pages). That is true, and this measures it. But it is only
half the story, and the other half is what a *cost model* exists to know:

    an index scan pays one random heap read per matching row

A query matching one row in a million reads three index pages and one heap
page. A query matching a third of the table reads three index pages and then
333,000 scattered heap reads — through pages a sequential scan would have read
once each. Somewhere between those, the index stops being worth using.

Milestone 5's planner chooses by *rule* — use an index whenever one covers a
comparison — so it gets the second case wrong on purpose, visibly. The numbers
below are the evidence Milestone 6's cost model will be built to act on.

Absolute times depend on the machine and the filesystem; the ratios are the
interesting part.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Column, Database, DataType, Schema
from engine.executor.engine import execute_script
from engine.executor.operators import IndexScan, SeqScan

ROW_COUNT = 20_000
PAGE_SIZE = 4096
REPEATS = 5
LOOKUPS = 200

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False, primary_key=True),
    Column("email", DataType.TEXT, nullable=False),
    Column("bucket", DataType.INTEGER, nullable=False),
)


def build(path: Path, *, with_index: bool) -> Database:
    db = Database.open(path, page_size=PAGE_SIZE)
    db.create_table("users", SCHEMA)
    db.insert_many(
        "users",
        [(n, f"user{n:06d}@example.com", n % 1000) for n in range(ROW_COUNT)],
    )
    if with_index:
        db.create_index("users_id", "users", "id", unique=True)
        db.create_index("users_bucket", "users", "bucket")
    db.sync()
    return db


def timed(fn, repeats: int = REPEATS) -> tuple[float, int]:
    """Median wall time in milliseconds, plus pages read on the last run."""
    samples: list[float] = []
    pages = 0
    for _ in range(repeats):
        started = time.perf_counter_ns()
        pages = fn()
        samples.append((time.perf_counter_ns() - started) / 1e6)
    return statistics.median(samples), pages


def run_query(db: Database, sql: str) -> tuple[int, int, str]:
    """Run one SELECT; return rows, pages read, and the access path used."""
    result = execute_script(sql, db)[0]
    path = "?"
    stack = [result.plan] if result.plan else []
    while stack:
        node = stack.pop()
        if isinstance(node, IndexScan):
            path = "IndexScan"
        elif isinstance(node, SeqScan):
            path = "SeqScan"
        stack.extend(node.children)
    return result.stats.rows_returned, result.stats.pages_read, path


def header(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def main() -> int:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        print(
            f"ChenDB index benchmark - {ROW_COUNT:,} rows, {PAGE_SIZE}-byte pages\n"
            f"Times are medians of {REPEATS} runs."
        )

        plain = build(root / "plain.chendb", with_index=False)
        indexed = build(root / "indexed.chendb", with_index=True)

        header("Build cost")
        # Charged once, and it is not free: one descent and one page write per
        # row, plus a split every half node.
        build_only = Database.open(root / "buildcost.chendb", page_size=PAGE_SIZE)
        build_only.create_table("users", SCHEMA)
        build_only.insert_many(
            "users",
            [(n, f"user{n:06d}@example.com", n % 1000) for n in range(ROW_COUNT)],
        )
        started = time.perf_counter_ns()
        build_only.create_index("users_id", "users", "id", unique=True)
        elapsed = (time.perf_counter_ns() - started) / 1e6
        tree = build_only.tree_for("users_id")
        print(
            f"  CREATE INDEX over {ROW_COUNT:,} rows   {elapsed:8.1f} ms   "
            f"height {tree.height}, {len(tree.page_ids())} pages, "
            f"{tree.stats.splits} splits"
        )
        print(
            "  Row by row, so O(n log n). A real system sorts first and packs "
            "leaves in one pass."
        )
        build_only.close()

        header("Point lookup — one row out of 20,000")
        for label, db in (("no index", plain), ("index", indexed)):
            def lookup(db: Database = db) -> int:
                total = 0
                for n in range(LOOKUPS):
                    _, pages, _ = run_query(
                        db, f"SELECT email FROM users WHERE id = {n * 97 % ROW_COUNT}"
                    )
                    total += pages
                return total // LOOKUPS

            millis, pages = timed(lookup, repeats=1)
            _, _, path = run_query(db, "SELECT email FROM users WHERE id = 7")
            print(
                f"  {label:<10} {millis / LOOKUPS:8.3f} ms/lookup   "
                f"{pages:5d} pages/lookup   {path}"
            )

        header("Selectivity — where the index stops paying")
        # bucket has 1000 distinct values over 20,000 rows, so `bucket < k`
        # matches roughly k/1000 of the table.
        print(f"  {'predicate':<28}{'rows':>8}{'no index':>12}{'index':>12}  path")
        for cutoff, note in (
            (1, "0.1% of rows"),
            (10, "1%"),
            (50, "5%"),
            (200, "20%"),
            (700, "70%"),
        ):
            sql = f"SELECT id FROM users WHERE bucket < {cutoff}"
            plain_ms, _ = timed(lambda sql=sql: run_query(plain, sql)[1])
            index_ms, index_pages = timed(lambda sql=sql: run_query(indexed, sql)[1])
            rows, _, path = run_query(indexed, sql)
            marker = "  <-- index is slower" if index_ms > plain_ms else ""
            print(
                f"  bucket < {cutoff:<19}{rows:>8}{plain_ms:>10.1f} ms"
                f"{index_ms:>10.1f} ms  {path}{marker}"
            )
            del note

        header("Pages read, which is what actually differs")
        for cutoff in (1, 50, 700):
            sql = f"SELECT id FROM users WHERE bucket < {cutoff}"
            _, plain_pages, _ = run_query(plain, sql)
            rows, index_pages, _ = run_query(indexed, sql)
            print(
                f"  bucket < {cutoff:<5} {rows:>6} rows    "
                f"scan {plain_pages:>6} pages    index {index_pages:>6} pages"
            )
        print(
            "\n  The scan reads every page once, whatever the predicate. The index\n"
            "  reads one heap page per matching row, and the same page repeatedly\n"
            "  when several matches share it — no buffer pool until Milestone 7."
        )

        header("Ordered scan")
        # An index scan emits rows in key order for free, which is the other
        # reason to build one. Nothing exploits it yet: there is no ORDER BY.
        tree = indexed.tree_for("users_id")
        started = time.perf_counter_ns()
        count = sum(1 for _ in tree.range_scan())
        elapsed = (time.perf_counter_ns() - started) / 1e6
        print(
            f"  full index scan  {elapsed:8.1f} ms   {count:,} entries in key order,\n"
            f"                   no sort — the reason ORDER BY on an indexed column\n"
            f"                   is free, once Milestone 6 knows to use it."
        )

        plain.close()
        indexed.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
