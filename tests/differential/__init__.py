"""Differential testing: the same query, asked of ChenDB and of SQLite.

Milestone 17. Every one of the 1,242 tests before this milestone was written by
whoever wrote the engine, so between them they test what its author thought of —
and the record says that is not enough. Milestone 11 turned up four real bugs by
accident. Milestone 12's brand-new CI failed on its first run, on a staleness that
had been there for a milestone. Six user-facing strings were wrong for a dozen
milestones. Milestone 16 shipped two bugs found only by trying it in a browser.

None of those were found by a test that was aimed at them, because you have to
think of the query before you can write the assertion. A second engine does not
have to think of anything: it just answers, and the two answers either match or
they do not. That is the whole idea, and on the first run it found seven bugs —
including `WHERE v` over an integer column silently matching *no rows*, and an
index scan and a sequential scan returning different rows for the same predicate.

    generator.py   random schemas, rows and typed queries, from a seed
    dialect.py     notation and representation, and nothing else
    engines.py     the two adapters, one Outcome
    oracle.py      whether two answers are the same answer
    registry.py    differences that are not bugs, as rules
    shrink.py      the smallest case that still fails the same way
    campaign.py    run, count, and report

The design constraint that shapes all of it: **a generated query whose answer is
not uniquely defined cannot be compared with anything.** Most of the work is in
knowing which those are — see :mod:`tests.differential.generator`.
"""
