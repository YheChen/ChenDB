"""``python -m engine`` — an interactive storage explorer.

Milestone 1 has no SQL parser, so this shell speaks dot-commands instead.  It
is a real client against a real file: everything it prints is read back from
disk, and ``.page`` shows the actual bytes.

``.create`` uses a deliberately un-SQL-like column syntax
(``name:TYPE!*``) so that nobody mistakes it for the parser.  Milestone 2
replaces it with ``CREATE TABLE``.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from engine import MILESTONE, MILESTONE_FEATURES, __version__
from engine.database import Database
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.errors import ChenDBError
from engine.executor.engine import execute_script
from engine.executor.operators import describe_plan
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType
from engine.storage.constants import DEFAULT_PAGE_SIZE
from engine.storage.heap import RecordId
from engine.storage.inspect import hexdump, render_page_map

_NULL_LITERAL = "NULL"
_TRUE_LITERALS = frozenset({"true", "t", "yes", "1"})
_FALSE_LITERALS = frozenset({"false", "f", "no", "0"})
_DEFAULT_SCAN_LIMIT = 50
_DEFAULT_EVENT_LIMIT = 20
_DEFAULT_HEX_BYTES = 256

_BANNER = f"""ChenDB {__version__} — Milestone {MILESTONE} \
({" + ".join(MILESTONE_FEATURES)})
Type .help for commands, .quit to exit. Anything not starting with '.' is SQL."""

_HELP = """\
  <sql>;                         run SQL — SELECT (with JOIN, GROUP BY,
                                 ORDER BY, LIMIT), INSERT, UPDATE, DELETE,
                                 CREATE TABLE/INDEX, EXPLAIN [ANALYZE], ANALYZE
  .help                          show this message
  .info                          database file and meta-page summary
  .tables                        every table, with row and page counts
  .use TABLE                     make TABLE the target of .schema/.scan/…
  .schema [TABLE]                a table's columns
  .create TABLE col:TYPE[!][*]   define a table  (! = NOT NULL, * = PRIMARY KEY)
                                 TYPE is INTEGER, FLOAT, BOOLEAN or TEXT
  .insert v1 | v2 | v3           insert one row  (NULL for a null value)
  .scan [limit]                  print rows
  .count                         live row count
  .delete PAGE SLOT              tombstone the record at (page, slot)
  .analyze [TABLE]               recompute planner statistics
  .stats-of [TABLE]              what the planner knows about a table
  .indexes                       every index, with height and size
  .tree NAME                     one B+ tree, level by level
  .find NAME VALUE               trace a point lookup through an index
  .pages                         one line per page in the file
  .page ID                       decoded header, slot directory and records
  .map ID                        ASCII map of a page's regions
  .hex ID [bytes]                raw hexdump of a page
  .stats                         cumulative pager I/O counters
  .trace LEVEL                   off | summary | operator | storage | verbose
  .events [n]                    last n diagnostic events
  .sync                          fsync the file
  .quit                          close and exit\
"""


class Shell:
    """A tiny dot-command REPL over one :class:`Database`."""

    def __init__(self, db: Database, sink: RingBufferSink) -> None:
        self.db = db
        self.sink = sink
        self._table: str | None = None

    # -- the current table -------------------------------------------------
    #
    # A database holds many tables from Milestone 4 on, but a REPL where every
    # command repeats the table name is tedious. One "current table", defaulting
    # to the only one when there is only one, keeps the short commands short.

    @property
    def table(self) -> str:
        if self._table is not None and self.db.table(self._table) is not None:
            return self._table
        names = self.db.table_names()
        if not names:
            raise ValueError("no tables yet; use .create or CREATE TABLE")
        if len(names) > 1 and self._table is None:
            raise ValueError(f"pick one with .use: {', '.join(names)}")
        self._table = names[0]
        return self._table

    # -- dispatch ----------------------------------------------------------

    def run_line(self, line: str) -> bool:
        """Execute one line. Returns ``False`` when the shell should exit."""
        line = line.strip()
        if not line or line.startswith("--"):
            return True
        if not line.startswith("."):
            self._run_sql(line)
            return True

        command, _, rest = line.partition(" ")
        # A hyphen reads better in a command than an underscore, and cannot be
        # part of a Python identifier, so it is translated here.
        handler = getattr(self, f"_cmd_{command[1:].replace('-', '_')}", None)
        if handler is None:
            print(f"unknown command {command!r}; try .help")
            return True
        try:
            handler(rest.strip())
        except ChenDBError as exc:
            print(f"error: {exc}")
        except (ValueError, IndexError) as exc:
            print(f"bad arguments: {exc}")
        return command not in (".quit", ".exit")

    # -- SQL ---------------------------------------------------------------

    def _run_sql(self, sql: str) -> None:
        """Run one or more statements and print whatever each produced."""
        try:
            results = execute_script(sql, self.db, tracer=self.db.tracer)
        except ChenDBError as exc:
            print(f"error: {exc}")
            return
        for result in results:
            if result.message:
                print(result.message)
            if not result.returns_rows:
                continue
            names = [column.name for column in result.columns]
            widths = [max(len(name), 12) for name in names]
            print("  ".join(name.ljust(w) for name, w in zip(names, widths, strict=True)))
            print("-" * (sum(widths) + 2 * (len(widths) - 1)))
            for row in result.rows:
                print(
                    "  ".join(
                        _render_value(value).ljust(w)
                        for value, w in zip(row, widths, strict=True)
                    )
                )
            plan = describe_plan(result.plan) if result.plan else ""
            print(
                f"({result.stats.rows_returned} row(s), "
                f"{result.stats.pages_read} page(s) read, "
                f"{result.stats.duration_ns / 1e6:.2f} ms)"
            )
            if plan:
                print(plan)

    # -- commands ----------------------------------------------------------

    def _cmd_help(self, _: str) -> None:
        print(_HELP)

    def _cmd_quit(self, _: str) -> None:
        self.db.close()
        print("closed.")

    _cmd_exit = _cmd_quit

    def _cmd_info(self, _: str) -> None:
        meta = self.db.pager.meta
        print(f"path         {self.db.path}")
        print(f"page size    {meta.page_size} bytes")
        print(f"pages        {meta.page_count}")
        print(f"file size    {meta.page_count * meta.page_size} bytes")
        print(f"format       version {meta.format_version}")
        print(f"free list    {_page_ref(meta.free_list_head)}")
        print(
            f"chendb_tables   {_page_ref(meta.catalog_tables_first)}"
            f" .. {_page_ref(meta.catalog_tables_last)}"
        )
        print(
            f"chendb_columns  {_page_ref(meta.catalog_columns_first)}"
            f" .. {_page_ref(meta.catalog_columns_last)}"
        )
        print(
            f"chendb_indexes  {_page_ref(meta.catalog_indexes_first)}"
            f" .. {_page_ref(meta.catalog_indexes_last)}"
        )
        print(f"next object id  {meta.next_object_id}")
        print(f"tables       {', '.join(self.db.table_names()) or '(none)'}")
        print(f"indexes      {', '.join(i.name for i in self.db.indexes()) or '(none)'}")

    def _cmd_tables(self, _: str) -> None:
        names = self.db.table_names()
        if not names:
            print("no tables yet; use .create or CREATE TABLE")
            return
        print(f"{'table':<20} {'cols':>5} {'rows':>8} {'pages':>6}  indexes")
        for name in names:
            info = self.db.require_table(name)
            heap = self.db.heap_for(name)
            indexes = ", ".join(i.name for i in self.db.indexes(name)) or "-"
            marker = "*" if name == self._table else " "
            print(
                f"{marker}{info.name:<19} {len(info.schema):>5} {heap.count():>8} "
                f"{heap.page_count():>6}  {indexes}"
            )

    def _cmd_use(self, rest: str) -> None:
        name = rest.strip()
        info = self.db.require_table(name)
        self._table = info.name
        print(f"current table is {info.name}")

    def _cmd_schema(self, rest: str) -> None:
        table = self.db.require_table(rest.strip() or self.table)
        print(f"TABLE {table.name}")
        for index, column in enumerate(table.schema):
            flags = "".join(
                [
                    "" if column.nullable else " NOT NULL",
                    " PRIMARY KEY" if column.primary_key else "",
                ]
            )
            print(f"  {index}  {column.name:<20} {column.data_type.sql_name}{flags}")
        print(f"  null bitmap: {table.schema.null_bitmap_size} byte(s)")
        fixed = table.schema.fixed_row_size
        print(
            f"  row size:    {fixed} bytes (fixed)" if fixed else "  row size:    variable"
        )
        for info in self.db.indexes(table.name):
            kind = "UNIQUE INDEX" if info.unique else "INDEX"
            print(f"  {kind} {info.name} ({info.column_name})")

    def _cmd_create(self, rest: str) -> None:
        parts = shlex.split(rest)
        if len(parts) < 2:
            raise ValueError("usage: .create TABLE col:TYPE[!][*] ...")
        name, specs = parts[0], parts[1:]
        schema = Schema(tuple(_parse_column_spec(spec) for spec in specs))
        descriptor = self.db.create_table(name, schema)
        self._table = descriptor.name
        print(f"created table {descriptor.name} with {len(schema)} column(s)")

    def _cmd_insert(self, rest: str) -> None:
        table = self.table
        schema = self.db.schema_of(table)
        raw_values = [part.strip() for part in rest.split("|")]
        values = [
            _parse_value(text, column.data_type)
            for text, column in zip(raw_values, schema, strict=True)
        ]
        record_id = self.db.insert(table, values)
        print(f"inserted at {record_id}")

    def _cmd_scan(self, rest: str) -> None:
        limit = int(rest) if rest else _DEFAULT_SCAN_LIMIT
        table = self.table
        schema = self.db.schema_of(table)
        header = ["rid".ljust(9)] + [name.ljust(16) for name in schema.column_names]
        print("  ".join(header))
        print("-" * (len("  ".join(header))))
        shown = 0
        for record_id, row in self.db.scan(table):
            if shown >= limit:
                print(f"... stopped at {limit} rows; .scan N for more")
                break
            cells = [str(record_id).ljust(9)] + [
                _render_value(value).ljust(16) for value in row
            ]
            print("  ".join(cells))
            shown += 1
        print(f"({shown} row(s))")

    def _cmd_count(self, _: str) -> None:
        print(self.db.count(self.table))

    def _cmd_delete(self, rest: str) -> None:
        page_id, slot_id = (int(part) for part in rest.split())
        deleted = self.db.delete(self.table, RecordId(page_id, slot_id))
        print("deleted" if deleted else "no live record there")

    # -- planner -----------------------------------------------------------

    def _cmd_analyze(self, rest: str) -> None:
        gathered = self.db.analyze(rest.strip() or None)
        for stats in gathered:
            print(
                f"analyzed {stats.table_name}: {stats.row_count} rows, "
                f"{stats.page_count} pages"
            )

    def _cmd_stats_of(self, rest: str) -> None:
        """What the planner reasons about, and how old it is."""
        name = rest.strip() or self.table
        stats = self.db.statistics.for_table(name)
        stale = " (STALE — run .analyze)" if self.db.statistics.is_stale(name) else ""
        print(
            f"{stats.table_name}: {stats.row_count} rows, {stats.page_count} pages{stale}"
        )
        print(f"  {'column':<18}{'distinct':>10}{'nulls':>8}  min / max")
        for column in stats.columns:
            span = (
                "-"
                if column.minimum is None
                else f"{str(column.minimum)[:18]} / {str(column.maximum)[:18]}"
            )
            print(
                f"  {column.name:<18}{column.distinct_count:>10}"
                f"{column.null_count:>8}  {span}"
            )

    # -- indexes -----------------------------------------------------------

    def _cmd_indexes(self, _: str) -> None:
        indexes = self.db.indexes()
        if not indexes:
            print("no indexes; try CREATE INDEX ix ON t (c)")
            return
        print(
            f"{'index':<22} {'on':<22} {'type':<8} {'h':>2} {'entries':>9} "
            f"{'pages':>6}  unique"
        )
        for info in indexes:
            tree = self.db.tree_for(info.name)
            print(
                f"{info.name:<22} {info.table_name + '.' + info.column_name:<22} "
                f"{info.data_type.sql_name:<8} {tree.height:>2} {tree.count():>9} "
                f"{len(tree.page_ids()):>6}  {'yes' if info.unique else '-'}"
            )

    def _cmd_tree(self, rest: str) -> None:
        """Print the tree level by level. The leaf chain is shown as arrows."""
        snapshot = self.db.tree_for(rest.strip()).snapshot()
        print(f"root page {snapshot.root_page_id}, height {snapshot.height}")
        if snapshot.truncated:
            print(f"(showing the first {len(snapshot.nodes)} nodes)")
        by_level: dict[int, list] = {}
        for node in snapshot.nodes:
            by_level.setdefault(node.level, []).append(node)
        for level in sorted(by_level, reverse=True):
            kind = "leaves" if level == 0 else f"level {level}"
            print(f"\n{kind}:")
            for node in by_level[level]:
                keys = " ".join(node.keys[:8])
                more = f" +{len(node.keys) - 8}" if len(node.keys) > 8 else ""
                arrow = f"  -> p{node.next_leaf_id}" if node.next_leaf_id else ""
                print(f"  p{node.page_id:<5} [{keys}{more}]{arrow}")

    def _cmd_find(self, rest: str) -> None:
        """Trace one point lookup: the path taken, and what it found."""
        name, _, raw = rest.strip().partition(" ")
        info = self.db.index(name)
        if info is None:
            known = ", ".join(i.name for i in self.db.indexes()) or "none"
            raise ValueError(f"no index named {name!r}; this database has {known}")
        value = _parse_value(raw.strip(), info.data_type)
        tree = self.db.tree_for(info.name)
        key = info.encode(value)
        before = self.db.stats.page_reads
        matches = tree.search(key)
        print(f"path    {' -> '.join(f'p{page}' for page in tree.descent_path(key))}")
        print(f"height  {tree.height}")
        print(f"pages   {self.db.stats.page_reads - before} read")
        print(
            f"found   {len(matches)} row(s): "
            f"{' '.join(str(rid) for rid in matches[:12])}"
            f"{' …' if len(matches) > 12 else ''}"
        )

    def _cmd_pages(self, _: str) -> None:
        print(
            f"{'id':>4}  {'type':<8} {'offset':>8} {'slots':>5} {'live':>5} "
            f"{'free':>6} {'dead':>6} {'next':>5}  {'ck':<3} owner"
        )
        for summary in self.db.page_summaries():
            print(
                f"{summary.page_id:>4}  {summary.page_type:<8} "
                f"{summary.file_offset:>8} {summary.slot_count:>5} "
                f"{summary.live_record_count:>5} {summary.free_space:>6} "
                f"{summary.reclaimable_space:>6} "
                f"{('-' if summary.next_page_id is None else summary.next_page_id):>5}  "
                f"{'ok' if summary.checksum_valid else 'BAD':<3} {summary.owner}"
            )

    def _cmd_page(self, rest: str) -> None:
        detail = self.db.page_detail(int(rest))
        print(render_page_map(detail))
        print("\nheader")
        for field in detail.header_fields:
            print(
                f"  {field.name:<16} @{field.offset:>3} {field.size}B  "
                f"{field.value!s:<24} 0x{field.raw_hex}"
            )
        if not detail.slots:
            return
        print("\nslot directory")
        for slot in detail.slots:
            if not slot.is_live:
                print(f"  slot {slot.slot_id:<3} <deleted>")
                continue
            print(
                f"  slot {slot.slot_id:<3} offset={slot.offset:<5} "
                f"length={slot.length:<5} {slot.raw_hex[:48]}"
                f"{'...' if len(slot.raw_hex) > 48 else ''}"
            )
            if slot.record is not None:
                for field in slot.record.fields:
                    location = (
                        "NULL" if field.is_null else f"@{field.offset}+{field.length}"
                    )
                    print(
                        f"        {field.name:<16} {field.type_name:<8} "
                        f"{location:<12} {_render_value(field.value)}"
                    )
            elif slot.decode_error:
                print(f"        decode error: {slot.decode_error}")

    def _cmd_map(self, rest: str) -> None:
        print(render_page_map(self.db.page_detail(int(rest))))

    def _cmd_hex(self, rest: str) -> None:
        parts = rest.split()
        page_id = int(parts[0])
        limit = int(parts[1]) if len(parts) > 1 else _DEFAULT_HEX_BYTES
        detail = self.db.page_detail(page_id)
        print(
            hexdump(
                detail.raw,
                start_offset=detail.summary.file_offset,
                limit=limit,
            )
        )

    def _cmd_stats(self, _: str) -> None:
        for key, value in self.db.stats.as_dict().items():
            print(f"  {key:<24} {value:>12,}")
        sink_stats = self.sink.stats
        print(
            f"  {'events retained':<24} {sink_stats.size:>12,}"
            f"  (dropped {sink_stats.dropped:,})"
        )

    def _cmd_trace(self, rest: str) -> None:
        if not rest:
            print(f"trace level is {self.db.tracer.level.name}")
            return
        self.db.tracer.level = TraceLevel[rest.strip().upper()]
        print(f"trace level set to {self.db.tracer.level.name}")

    def _cmd_events(self, rest: str) -> None:
        limit = int(rest) if rest else _DEFAULT_EVENT_LIMIT
        records = self.sink.snapshot()[-limit:]
        if not records:
            print("no events; raise the level with .trace storage")
            return
        for item in records:
            print(
                f"  #{item.seq:<5} {item.category:<10} {item.event_type:<22} {item.event}"
            )

    def _cmd_sync(self, _: str) -> None:
        self.db.sync()
        print("synced")


def _parse_column_spec(spec: str) -> Column:
    """Parse ``name:TYPE!*`` into a :class:`Column`."""
    name, _, decorated = spec.partition(":")
    if not name or not decorated:
        raise ValueError(f"bad column spec {spec!r}; expected name:TYPE[!][*]")
    primary_key = decorated.endswith("*")
    decorated = decorated.removesuffix("*")
    not_null = decorated.endswith("!")
    type_name = decorated.removesuffix("!")
    return Column(
        name=name,
        data_type=DataType.from_sql_name(type_name),
        nullable=not (not_null or primary_key),
        primary_key=primary_key,
    )


def _parse_value(text: str, data_type: DataType) -> Any:
    """Convert one CLI token into a typed value."""
    if text == _NULL_LITERAL:
        return None
    match data_type:
        case DataType.INTEGER:
            return int(text)
        case DataType.FLOAT:
            return float(text)
        case DataType.BOOLEAN:
            lowered = text.casefold()
            if lowered in _TRUE_LITERALS:
                return True
            if lowered in _FALSE_LITERALS:
                return False
            raise ValueError(f"{text!r} is not a boolean")
        case DataType.TEXT:
            return text.strip("'\"")
    raise ValueError(f"unsupported type {data_type!r}")


def _render_value(value: Any) -> str:
    return "NULL" if value is None else str(value)


def _page_ref(page_id: int) -> str:
    return "-" if page_id == 0xFFFFFFFF else str(page_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m engine",
        description="ChenDB interactive storage and index explorer.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="chendb.chendb",
        type=Path,
        help="database file to open (created if missing)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"page size for a new file (default {DEFAULT_PAGE_SIZE})",
    )
    parser.add_argument(
        "--trace",
        default="summary",
        choices=[level.name.lower() for level in TraceLevel],
        help="initial diagnostics verbosity (default summary)",
    )
    parser.add_argument(
        "--trace-capacity",
        type=int,
        default=10_000,
        help="how many diagnostic events to retain (default 10000)",
    )
    parser.add_argument(
        "-c",
        "--command",
        action="append",
        default=[],
        metavar="CMD",
        help="run a dot-command and exit; repeatable",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    sink = RingBufferSink(capacity=args.trace_capacity)
    tracer = Tracer(sink, TraceLevel[args.trace.upper()])
    db = Database.open(args.path, page_size=args.page_size, tracer=tracer)
    shell = Shell(db, sink)

    try:
        if args.command:
            for command in args.command:
                if not shell.run_line(command):
                    break
            return 0

        print(_BANNER)
        print(f"opened {db.path} ({db.page_count} page(s))")
        while True:
            try:
                line = input("chendb> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not shell.run_line(line):
                break
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
