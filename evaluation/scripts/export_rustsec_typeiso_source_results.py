#!/usr/bin/env python3
"""Export bounded Type Isolation results from the frozen source-witness sweep.

The source sweep validates vulnerable system oracles and patched controls, but
it deliberately records compiler critical-site coverage separately.  This
exporter turns selected sweep scenarios into explicit mechanism-ledger rows so
an executed witness is not confused with an unrun candidate.  A row remains
``inconclusive`` until the exact victim allocation and replacement allocation
are covered by compiler-provided identities; clean native execution alone is
never treated as mitigation or detection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]


class ResultError(RuntimeError):
    """Raised when source-sweep evidence cannot support a bounded result."""


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResultError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ResultError(f"{path} must contain a JSON object")
    return value


def artifact(path: pathlib.Path) -> dict[str, Any]:
    resolved = path.resolve()
    payload = resolved.read_bytes()
    try:
        display = str(resolved.relative_to(ROOT))
    except ValueError:
        display = str(resolved)
    return {
        "path": display,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResultError(f"{label} must be a non-negative integer")
    return value


def variant_record(scenario: dict[str, Any], archive: str, variant: str) -> dict[str, Any]:
    archive_record = scenario.get(archive)
    if not isinstance(archive_record, dict):
        raise ResultError(f"scenario {scenario.get('scenario_id')} lacks {archive}")
    value = archive_record.get(variant)
    if not isinstance(value, dict):
        raise ResultError(
            f"scenario {scenario.get('scenario_id')} lacks {archive}/{variant}"
        )
    return value


def system_baseline_valid(record: dict[str, Any], minimum_repetitions: int) -> bool:
    repetitions = nonnegative_int(record.get("executed_repetitions"), "system repetitions")
    findings = nonnegative_int(record.get("tool_finding_count"), "system findings")
    return (
        record.get("execution_status") == "completed"
        and repetitions >= minimum_repetitions
        and findings == repetitions
        and record.get("oracle_status") == "tool_finding_observed"
    )


def patched_system_valid(record: dict[str, Any], minimum_repetitions: int) -> bool:
    repetitions = nonnegative_int(
        record.get("executed_repetitions"), "patched system repetitions"
    )
    findings = nonnegative_int(record.get("tool_finding_count"), "patched findings")
    status = record.get("oracle_status")
    runtime_control = (
        repetitions >= minimum_repetitions
        and findings == 0
        and status in {"clean_tool_runs_observed", "patched_safe_panic_control_observed"}
    )
    compile_control = repetitions == 0 and status == "patched_control_compile_rejection_observed"
    return record.get("execution_status") == "completed" and (
        runtime_control or compile_control
    )


def evaluate_scenario(
    scenario: dict[str, Any],
    *,
    advisory_id: str,
    minimum_repetitions: int,
) -> dict[str, Any]:
    scenario_id = scenario.get("scenario_id")
    case_id = scenario.get("case_id")
    if not isinstance(scenario_id, str) or not isinstance(case_id, str):
        raise ResultError("source-sweep scenario requires scenario_id and case_id")

    vulnerable_system = variant_record(scenario, "vulnerable", "system")
    vulnerable_plain = variant_record(scenario, "vulnerable", "typed_plain")
    vulnerable_typeiso = variant_record(scenario, "vulnerable", "typeiso")
    patched_system = variant_record(scenario, "patched", "system")
    patched_typeiso = variant_record(scenario, "patched", "typeiso")

    plain_repetitions = nonnegative_int(
        vulnerable_plain.get("executed_repetitions"), "typed_plain repetitions"
    )
    typeiso_repetitions = nonnegative_int(
        vulnerable_typeiso.get("executed_repetitions"), "typeiso repetitions"
    )
    patched_typeiso_repetitions = nonnegative_int(
        patched_typeiso.get("executed_repetitions"), "patched typeiso repetitions"
    )
    coverage_validated = (
        scenario.get("vulnerable", {}).get("typeiso", {}).get(
            "critical_site_coverage_validated"
        )
        is True
        and scenario.get("patched", {}).get("typeiso", {}).get(
            "critical_site_coverage_validated"
        )
        is True
    )
    checks = {
        "vulnerable_system_oracle_reproduced": system_baseline_valid(
            vulnerable_system, minimum_repetitions
        ),
        "patched_system_control_valid": patched_system_valid(
            patched_system, minimum_repetitions
        ),
        "typed_plain_arm_completed": vulnerable_plain.get("execution_status")
        == "completed"
        and plain_repetitions >= minimum_repetitions,
        "typeiso_arm_completed": vulnerable_typeiso.get("execution_status")
        == "completed"
        and typeiso_repetitions >= minimum_repetitions,
        "patched_typeiso_arm_completed": patched_typeiso.get("execution_status")
        == "completed"
        and (
            patched_typeiso_repetitions >= minimum_repetitions
            or patched_typeiso.get("oracle_status")
            == "native_diagnostic_compile_rejection_observed"
        ),
        "compiler_critical_site_coverage_validated": coverage_validated,
    }

    if not checks["vulnerable_system_oracle_reproduced"] or not checks[
        "patched_system_control_valid"
    ]:
        reason = "matched_system_evidence_requirements_not_met"
    elif not coverage_validated:
        reason = "compiler_critical_site_coverage_not_validated"
    else:
        reason = "type_isolation_specific_effect_not_validated"

    return {
        "advisory_id": advisory_id,
        "case_id": case_id,
        "scenario_id": scenario_id,
        "mechanism": "type_isolation",
        "outcome": "inconclusive",
        "true_positive": False,
        "result_semantics": "evidence_gap",
        "positive_scope": None,
        "negative_boundary": None,
        "reason": reason,
        "repetitions": min(plain_repetitions, typeiso_repetitions),
        "checks": checks,
        "observations": {
            "typed_plain_native_diagnostic_signal_count": nonnegative_int(
                vulnerable_plain.get("native_diagnostic_signal_count"),
                "typed_plain native signals",
            ),
            "typeiso_native_diagnostic_signal_count": nonnegative_int(
                vulnerable_typeiso.get("native_diagnostic_signal_count"),
                "typeiso native signals",
            ),
            "typed_plain_compiler_rewrites": nonnegative_int(
                vulnerable_plain.get("compiler_rewrites"), "typed_plain rewrites"
            ),
            "typeiso_compiler_rewrites": nonnegative_int(
                vulnerable_typeiso.get("compiler_rewrites"), "typeiso rewrites"
            ),
        },
        "claim_scope": "source witness with compiler critical-site coverage gate",
        "evidence_scope": "source_vulnerability_sweep",
        "source_vulnerability_detection_validated": False,
    }


def corpus_advisories(corpus: dict[str, Any]) -> dict[str, str]:
    cases = corpus.get("cases")
    if not isinstance(cases, list):
        raise ResultError("corpus cases must be a list")
    result: dict[str, str] = {}
    for case in cases:
        if not isinstance(case, dict):
            raise ResultError("corpus case must be an object")
        case_id = case.get("case_id")
        advisory_id = case.get("advisory_id")
        if not isinstance(case_id, str) or not isinstance(advisory_id, str):
            raise ResultError("corpus case requires case_id and advisory_id")
        if case_id in result:
            raise ResultError(f"duplicate corpus case: {case_id}")
        result[case_id] = advisory_id
    return result


def export(
    summary: dict[str, Any],
    corpus: dict[str, Any],
    *,
    scenario_ids: list[str],
    minimum_repetitions: int,
    summary_artifact: dict[str, Any],
    corpus_artifact: dict[str, Any],
) -> dict[str, Any]:
    if summary.get("schema_version") != 2 or summary.get("source") != (
        "unialloc-rustsec-heap-full-sweep-summary"
    ):
        raise ResultError("unsupported source-sweep summary")
    raw_scenarios = summary.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise ResultError("source-sweep scenarios must be a list")
    if not scenario_ids:
        raise ResultError("at least one --scenario is required")
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ResultError("duplicate --scenario selection")

    index: dict[str, dict[str, Any]] = {}
    for scenario in raw_scenarios:
        if not isinstance(scenario, dict):
            raise ResultError("source-sweep scenario must be an object")
        scenario_id = scenario.get("scenario_id")
        if not isinstance(scenario_id, str):
            raise ResultError("source-sweep scenario requires scenario_id")
        if scenario_id in index:
            raise ResultError(f"duplicate source-sweep scenario: {scenario_id}")
        index[scenario_id] = scenario

    advisories = corpus_advisories(corpus)
    results = []
    for scenario_id in scenario_ids:
        scenario = index.get(scenario_id)
        if scenario is None:
            raise ResultError(f"unknown source-sweep scenario: {scenario_id}")
        case_id = scenario.get("case_id")
        advisory_id = advisories.get(case_id)
        if advisory_id is None:
            raise ResultError(f"source-sweep case absent from corpus: {case_id}")
        results.append(
            {
                **evaluate_scenario(
                    scenario,
                    advisory_id=advisory_id,
                    minimum_repetitions=minimum_repetitions,
                ),
                "evidence": summary_artifact,
            }
        )

    results.sort(key=lambda row: (row["advisory_id"], row["scenario_id"]))
    return {
        "schema_version": 1,
        "source": "unialloc-rustsec-mechanism-results",
        "claim_grade": False,
        "boundary": (
            "These rows distinguish executed source witnesses from unrun candidates. "
            "They remain inconclusive until compiler critical-site identity coverage "
            "and a Type-Isolation-specific effect are validated."
        ),
        "minimum_repetitions": minimum_repetitions,
        "source_sweep_input": summary_artifact,
        "corpus_input": corpus_artifact,
        "counts": {"inconclusive": len(results)},
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=pathlib.Path, required=True)
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--minimum-repetitions", type=int, default=2)
    parser.add_argument("--output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.minimum_repetitions <= 0:
            raise ResultError("minimum repetitions must be positive")
        summary_path = args.summary.expanduser().resolve()
        corpus_path = args.corpus.expanduser().resolve()
        result = export(
            load_json(summary_path),
            load_json(corpus_path),
            scenario_ids=args.scenario,
            minimum_repetitions=args.minimum_repetitions,
            summary_artifact=artifact(summary_path),
            corpus_artifact=artifact(corpus_path),
        )
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (OSError, ResultError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
