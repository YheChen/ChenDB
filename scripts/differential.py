#!/usr/bin/env python3
"""Long differential runs, outside pytest.

    scripts/differential.py --seeds 0:5000            # a campaign
    scripts/differential.py --seed 1731 --verbose     # one case, everything shown

Exits non-zero if anything diverged. Prints the coverage table either way — the
counters are how you notice the suite has quietly stopped testing anything, which
is a failure mode that otherwise looks exactly like success.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.differential import campaign, generator, shrink


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, help="run exactly one seed")
    parser.add_argument("--seeds", default="0:64", help="a range, START:STOP")
    parser.add_argument(
        "--stop-after", type=int, default=0, help="give up after N failures"
    )
    parser.add_argument("--verbose", action="store_true", help="print every query")
    parser.add_argument("--no-shrink", action="store_true")
    args = parser.parse_args()

    if args.seed is not None:
        seeds = [args.seed]
    else:
        start, _, stop = args.seeds.partition(":")
        seeds = list(range(int(start), int(stop or int(start) + 1)))

    report = campaign.Report()
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        for seed in seeds:
            case = generator.case(seed)
            comparisons = campaign.run_case(case, root)
            report.absorb(seed, comparisons)
            if args.verbose:
                print(f"--- seed {seed}")
                for line in case.schema.setup(generator.CHENDB):
                    print(f"    {line}")
                for item in comparisons:
                    print(f"    [{item.verdict}] {item.query.sql}")
            if args.stop_after and len(report.failures) >= args.stop_after:
                break

        print(report.render())
        for seed, failure in report.failures[:5]:
            case = generator.case(seed)
            if args.no_shrink:
                print(campaign.render_failure(case, failure, seed=seed, steps=0))
                continue
            smallest, steps = shrink.shrink(
                case, failure.signature(), lambda c: campaign.run_case(c, root)
            )
            again = [i for i in campaign.run_case(smallest, root) if i.fails]
            culprit = next(
                (i for i in again if i.signature() == failure.signature()), failure
            )
            print()
            print(campaign.render_failure(smallest, culprit, seed=seed, steps=steps))

    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
