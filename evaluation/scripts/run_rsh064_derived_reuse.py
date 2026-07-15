#!/usr/bin/env python3
"""Run the pinned RSH-064 derived cross-identity reuse experiment.

This narrow entry point keeps the derived allocator experiment tied to the
same dedicated catalog as the published Neon Node/V8 witness while delegating
the six-arm evidence contract to the shared RustSec experiment runner.
"""

from __future__ import annotations

import pathlib
import sys


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_rsh064_neon_witness as neon_runner  # noqa: E402
import run_rustsec_heap_experiment as experiment  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = ROOT / "evaluation" / "config" / "rustsec_heap_neon_node_harnesses.json"
SCENARIO = "RSH-064-derived-reuse"


def forwarded_args(argv: list[str]) -> list[str]:
    if any(
        value in ("--catalog", "--scenario")
        or value.startswith("--catalog=")
        or value.startswith("--scenario=")
        for value in argv
    ):
        raise ValueError("the dedicated RSH-064 catalog and scenario are pinned")
    return ["--catalog", str(CATALOG), "--scenario", SCENARIO, *argv]


def main(argv: list[str] | None = None) -> int:
    selected = list(sys.argv[1:] if argv is None else argv)
    try:
        neon_runner.load_catalog(CATALOG)
        return experiment.main(forwarded_args(selected))
    except (
        ValueError,
        OSError,
        neon_runner.NeonHarnessError,
        neon_runner.harness.HarnessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
