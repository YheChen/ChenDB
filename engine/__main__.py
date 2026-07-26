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

from engine import __version__
from engine.database import Database
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.errors import ChenDBError
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

_BANNER = f"""ChenDB {__version__} — Milestone 1 (storage engine)
Type .help for commands, .quit to exit."""

_HELP = """\
  .help                          show this message
  .info                          database file and meta-page summary
  .schema                        the table's columns
  .create TABLE col:TYPE[!][*]   define the table  (! = NOT NULL, * = PRIMARY KEY)
                                 TYPE is INTEGER, FLOAT, BOOLEAN or TEXT
  .insert v1 | v2 | v3           insert one row  (NULL for a null value)
  .scan [limit]                  print rows
  .count                         live row count
  .delete PAGE SLOT              tombstone the record at (page, slot)
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

    # -- dispatch ----------------------------------------------------------

    def run_line(self, line: str) -> bool:
        """Execute one line. Returns ``False`` when the shell should exit."""
        line = line.strip()
        if not line or line.startswith("--"):
            return True
        if not line.startswith("."):
            print(
                "Milestone 1 has no SQL parser yet — that is Milestone 2.\n"
                "Use dot-commands; .help lists them."
            )
            return True

        command, _, rest = line.partition(" ")
        handler = getattr(self, f"_cmd_{command[1:]}", None)
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

    # -- commands ----------------------------------------------------------

    def _cmd_help(self, _: str) -> None:
        print(_HELP)

    def _cmd_quit(self, _: str) -> None:
        self.db.close()
        print("closed.")

    _cmd_exit = _cmd_quit

    def _cmd_info(self, _: str) -> None:
        meta = self.db.pager.meta
        table = self.db.table
        print(f"path         {self.db.path}")
        print(f"page size    {meta.page_size} bytes")
        print(f"pages        {meta.page_count}")
        print(f"file size    {meta.page_count * meta.page_size} bytes")
        print(f"format       version {meta.format_version}")
        print(f"free list    {_page_ref(meta.free_list_head)}")
        print(f"schema page  {_page_ref(meta.schema_page_id)}")
        print(
            f"heap pages   {_page_ref(meta.heap_first_page)}"
            f" .. {_page_ref(meta.heap_last_page)}"
        )
        print(f"table        {table.name if table else '(none)'}")

    def _cmd_schema(self, _: str) -> None:
        table = self.db.table
        if table is None:
            print("no table yet; use .create")
            return
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
        print(f"  row size:    {fixed} bytes (fixed)" if fixed else "  row size:    variable")

    def _cmd_create(self, rest: str) -> None:
        parts = shlex.split(rest)
        if len(parts) < 2:
            raise ValueError("usage: .create TABLE col:TYPE[!][*] ...")
        name, specs = parts[0], parts[1:]
        schema = Schema(tuple(_parse_column_spec(spec) for spec in specs))
        descriptor = self.db.create_table(name, schema)
        print(f"created table {descriptor.name} with {len(schema)} column(s)")

    def _cmd_insert(self, rest: str) -> None:
        schema = self.db.schema
        raw_values = [part.strip() for part in rest.split("|")]
        values = [
            _parse_value(text, column.data_type)
            for text, column in zip(raw_values, schema, strict=True)
        ]
        record_id = self.db.insert(values)
        print(f"inserted at {record_id}")

    def _cmd_scan(self, rest: str) -> None:
        limit = int(rest) if rest else _DEFAULT_SCAN_LIMIT
        schema = self.db.schema
        header = ["rid".ljust(9)] + [name.ljust(16) for name in schema.column_names]
        print("  ".join(header))
        print("-" * (len("  ".join(header))))
        shown = 0
        for record_id, row in self.db.scan():
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
        print(self.db.count())

    def _cmd_delete(self, rest: str) -> None:
        page_id, slot_id = (int(part) for part in rest.split())
        deleted = self.db.delete(RecordId(page_id, slot_id))
        print("deleted" if deleted else "no live record there")

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
            print(f"  #{item.seq:<5} {item.category:<10} {item.event_type:<22} {item.event}")

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
        description="ChenDB interactive storage explorer (Milestone 1).",
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
