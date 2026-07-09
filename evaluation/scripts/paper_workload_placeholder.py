#!/usr/bin/env python3
"""Fail-safe placeholder for generated paper performance plans.

The generated plan skeleton is meant to be structurally complete, but it must not
create synthetic performance evidence. Replace each generated command with the
real workload harness command, or generate a new plan with
`evaluate.py generate-paper-performance-plan --command-template ...`.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explain that a generated UniAlloc paper workload command must be replaced."
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature", default="")
    args = parser.parse_args()
    diagnostic = {
        "error": "paper workload placeholder command was executed",
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature or None,
        "required_action": (
            "replace this command with a real benchmark wrapper that emits "
            "JSON containing a finite seconds value on stdout"
        ),
    }
    print(json.dumps(diagnostic, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
