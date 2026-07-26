"""Disk-backed B+ tree indexes.

    key.py        order-preserving key encoding — the crux; read this first
    node.py       one page interpreted as a tree node
    bplustree.py  BPlusTree — search, insert, split, range scan, delete

    from engine import Database

    with Database.open("shop.chendb") as db:
        db.create_index("users_email", "users", "email", unique=True)
        db.lookup("users_email", "ada@example.com")

The engine reaches indexes through the catalog rather than this package
directly: :meth:`engine.catalog.catalog.Catalog.tree_for` opens the tree rooted
at the page recorded in ``chendb_indexes``, and
:meth:`engine.catalog.catalog.Catalog.indexes_on` is what the planner asks when
it wants to know whether a column has an access path.
"""

from engine.index.bplustree import BPlusTree, IndexStats, NodeSnapshot, TreeSnapshot
from engine.index.key import decode_key, describe_key, encode_key
from engine.index.node import BTreeNode, Entry

__all__ = [
    "BPlusTree",
    "BTreeNode",
    "Entry",
    "IndexStats",
    "NodeSnapshot",
    "TreeSnapshot",
    "decode_key",
    "describe_key",
    "encode_key",
]
