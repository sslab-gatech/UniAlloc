#!/usr/bin/env python3
"""Emit a structured non-claim JSON record for paper workload cells blocked on this host.

This helper is intentionally not a benchmark runner.  It gives the evaluation
plan an auditable, non-template command for cells such as ptmalloc/scudo that
cannot be executed on the current host/toolchain, without fabricating timing.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature")
    parser.add_argument("--run-index", type=int)
    parser.add_argument("--capability-kind", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--required-remediation", action="append", default=[])
    parser.add_argument("--required-evidence", action="append", default=[])
    args = parser.parse_args()

    record = {
        "schema_version": 1,
        "source": "paper-blocked-workload-contract",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked_not_runnable_on_this_host",
        "claim_grade": False,
        "complete_for_claim": False,
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "run_index": args.run_index,
        "capability_kind": args.capability_kind,
        "reason": args.reason,
        "required_remediation": args.required_remediation,
        "required_evidence": args.required_evidence,
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "claim_grade_blockers": [args.reason],
        "timing": None,
        "seconds": None,
    }
    print(json.dumps(record, sort_keys=True))
    return 42


if __name__ == "__main__":
    sys.exit(main())
