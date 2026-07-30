"""The write-ahead log, what makes a commit survive the power going out.

    engine/wal/
      record.py    one entry: type, LSN, page id, before- and after-images
      log.py       the file: append, flush, scan, checkpoint, truncate
      recovery.py  analysis → redo → undo, on open

Milestone 8 made writes atomic against *errors*. This makes them atomic against
*power loss*, and the whole difference is one small durable write: a commit
record. Without it nothing on disk distinguishes a transaction that finished
from one that was interrupted, however carefully the pages were ordered.
"""

from engine.wal.log import WAL_SUFFIX, WalStats, WriteAheadLog
from engine.wal.record import (
    NO_TRANSACTION,
    RECORD_HEADER_SIZE,
    LogRecord,
    RecordType,
    decode_record,
)
from engine.wal.recovery import RecoveryReport, recover

__all__ = [
    "NO_TRANSACTION",
    "RECORD_HEADER_SIZE",
    "WAL_SUFFIX",
    "LogRecord",
    "RecordType",
    "RecoveryReport",
    "WalStats",
    "WriteAheadLog",
    "decode_record",
    "recover",
]
