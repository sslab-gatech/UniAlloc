#!/usr/bin/env python3
"""Sweep the pinned RustSec heap experiment matrix across catalog scenarios.

This wrapper only orchestrates the existing per-scenario experiment runner. The
``preflight`` stage is topology/materialization validation. The ``run`` stage
executes unsafe witnesses and requires ``--execute-unsafe``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Iterable, Sequence


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "evaluation" / "config" / "rustsec_heap_harnesses.json"
EXPERIMENT_RUNNER = SCRIPT_DIR / "run_rustsec_heap_experiment.py"
REALWORLD_HELPER = SCRIPT_DIR / "realworld_type_isolation_matrix.py"
PASS_SOURCE = (
    ROOT / "tools" / "unialloc-rustc-pass" / "unialloc-rustc-mir-rewrite-dry-run.rs"
)
VALID_SCENARIO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SWEEP_SCHEMA = 2
BINDING_SCHEMA = 1
VARIANTS = ("system", "unialloc", "typed_plain", "typeiso")
ARCHIVE_VARIANTS = ("vulnerable", "patched")
UNSUPPORTED_STATUSES = {
    "unsupported_tool_allocator_pair",
    "unsupported_allocator_topology",
}
OBSERVED_COMPILE_REJECTION_STATUSES = {
    "native_diagnostic_compile_rejection_observed",
    "patched_control_compile_rejection_observed",
}
COUNT_KEYS = (
    "arm_count",
    "terminal_arm_count",
    "expected_compile_rejection_count",
    "planned_repetition_slots",
    "executed_repetitions",
    "run_count",
    "unsupported_count",
    "unexpected_count",
)


class SweepError(RuntimeError):
    """A fail-closed sweep configuration or result error."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_sha256(value: object) -> str:
    return sha256_bytes(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    )


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def current_unialloc_implementation_digest() -> str:
    """Match the implementation digest embedded by the experiment runner."""

    roots = (ROOT / "unialloc", ROOT / "alloc_macros")
    files = [ROOT / "Cargo.toml", ROOT / "Cargo.lock", PASS_SOURCE, REALWORLD_HELPER]
    for source_root in roots:
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and "target" not in path.parts
            and (path.suffix in {".rs", ".toml"} or path.name == "build.rs")
        )
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def capture_input_hashes(args: argparse.Namespace) -> dict[str, str]:
    """Capture every mutable input used to decide whether results can resume."""

    return {
        "catalog": sha256_file(args.catalog),
        "runner": sha256_file(EXPERIMENT_RUNNER),
        "script": sha256_file(pathlib.Path(__file__).resolve()),
        "unialloc_implementation": current_unialloc_implementation_digest(),
    }


def detect_input_drift(
    args: argparse.Namespace, before: dict[str, str]
) -> tuple[dict[str, str | None], dict[str, dict[str, str | None]]]:
    try:
        after: dict[str, str | None] = capture_input_hashes(args)
    except OSError:
        paths = {
            "catalog": args.catalog,
            "runner": EXPERIMENT_RUNNER,
            "script": pathlib.Path(__file__).resolve(),
        }
        after = {}
        for name, path in paths.items():
            try:
                after[name] = sha256_file(path)
            except OSError:
                after[name] = None
        try:
            after["unialloc_implementation"] = current_unialloc_implementation_digest()
        except OSError:
            after["unialloc_implementation"] = None
    drift = {
        name: {"before": value, "after": after.get(name)}
        for name, value in before.items()
        if after.get(name) != value
    }
    return after, drift


def validate_scenario_id(value: str) -> str:
    if not VALID_SCENARIO_RE.fullmatch(value):
        raise SweepError(f"invalid scenario id: {value!r}")
    pure = pathlib.PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SweepError(f"invalid scenario id path segment: {value!r}")
    return value


def parse_csv(value: str, allowed: Iterable[str], label: str) -> tuple[str, ...]:
    allowed_values = tuple(allowed)
    selected = tuple(part.strip() for part in value.split(",") if part.strip())
    if not selected:
        raise SweepError(f"{label} must contain at least one value")
    unknown = sorted(set(selected) - set(allowed_values))
    if unknown:
        raise SweepError(f"unknown {label}: {', '.join(unknown)}")
    if len(set(selected)) != len(selected):
        raise SweepError(f"duplicate {label} values")
    return selected


def runner_command_base(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        str(EXPERIMENT_RUNNER),
        "--catalog",
        str(args.catalog),
        "--cache",
        str(args.cache),
        "--variants",
        args.variants,
        "--archive-variants",
        args.archive_variants,
        "--repetitions",
        str(args.repetitions),
        "--jobs",
        str(args.jobs),
        "--build-timeout",
        str(args.build_timeout),
        "--run-timeout",
        str(args.run_timeout),
    ]


def load_scenarios(args: argparse.Namespace) -> list[dict[str, Any]]:
    command = runner_command_base(args) + ["--action", "list"]
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise SweepError(
            "experiment runner list failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    payload = json.loads(completed.stdout)
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        raise SweepError("experiment runner list payload has no scenarios list")
    selected = {validate_scenario_id(item) for item in args.scenario}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(
            scenario.get("scenario_id"), str
        ):
            raise SweepError("invalid scenario record in list payload")
        scenario_id = validate_scenario_id(scenario["scenario_id"])
        if scenario_id in seen:
            raise SweepError(f"duplicate scenario in list payload: {scenario_id}")
        seen.add(scenario_id)
        if selected and scenario_id not in selected:
            continue
        records.append({**scenario, "scenario_id": scenario_id})
    missing = sorted(selected - {record["scenario_id"] for record in records})
    if missing:
        raise SweepError(f"unknown scenario filters: {', '.join(missing)}")
    return records


def scenario_output_dir(root: pathlib.Path, scenario_id: str) -> pathlib.Path:
    validate_scenario_id(scenario_id)
    output = (root / scenario_id).resolve()
    root_resolved = root.resolve()
    if root_resolved not in output.parents or output == root_resolved:
        raise SweepError(f"scenario output escaped sweep root: {scenario_id!r}")
    return output


def expected_result_path(stage: str, output_dir: pathlib.Path) -> pathlib.Path:
    return output_dir / (
        "preflight.json" if stage == "preflight" else "experiment.json"
    )


def result_binding_path(result_path: pathlib.Path) -> pathlib.Path:
    return result_path.with_name(result_path.name + ".sweep-binding.json")


def expected_pairs(
    variants: Sequence[str], archive_variants: Sequence[str]
) -> set[tuple[str, str]]:
    return {
        (archive, allocator) for archive in archive_variants for allocator in variants
    }


def validate_matrix_records(
    records: object,
    *,
    variants: Sequence[str],
    archive_variants: Sequence[str],
    label: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(records, list):
        raise SweepError(f"{label} must be a list")
    expected = expected_pairs(variants, archive_variants)
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise SweepError(f"{label} contains a non-object record")
        archive = record.get("archive_variant")
        allocator = record.get("allocator_variant")
        pair = (archive, allocator)
        if pair in observed:
            raise SweepError(f"duplicate matrix pair in {label}: {archive}/{allocator}")
        if pair not in expected:
            raise SweepError(
                f"unexpected matrix pair in {label}: {archive}/{allocator}"
            )
        observed[pair] = record
    missing = sorted(expected - set(observed))
    if missing:
        rendered = ", ".join(f"{archive}/{allocator}" for archive, allocator in missing)
        raise SweepError(f"incomplete matrix in {label}; missing: {rendered}")
    return observed


def validate_preflight_payload(
    payload: object,
    *,
    scenario_id: str,
    variants: Sequence[str],
    archive_variants: Sequence[str],
    repetitions: int,
    input_hashes: dict[str, str],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SweepError("preflight result must be a JSON object")
    if payload.get("scenario_id") != scenario_id:
        raise SweepError("preflight scenario id mismatch")
    if payload.get("repetitions_requested") != repetitions:
        raise SweepError("preflight repetition count mismatch")
    if payload.get("claim_grade") is not False:
        raise SweepError("preflight claim_grade must be false")
    if payload.get("unsafe_execution_requested") is not False:
        raise SweepError("preflight unsafe execution marker mismatch")
    if payload.get("catalog_sha256") != input_hashes["catalog"]:
        raise SweepError("preflight catalog hash mismatch")
    planned = validate_matrix_records(
        payload.get("planned_arms"),
        variants=variants,
        archive_variants=archive_variants,
        label="preflight planned_arms",
    )
    for pair, arm in planned.items():
        if not isinstance(arm.get("supported"), bool):
            raise SweepError(
                f"preflight supported marker missing for {pair[0]}/{pair[1]}"
            )
    return payload


def validate_arm_fingerprint(
    arm: dict[str, Any],
    *,
    scenario_id: str,
    pair: tuple[str, str],
    input_hashes: dict[str, str],
) -> None:
    payload = arm.get("fingerprint_payload")
    fingerprint = arm.get("fingerprint")
    if not isinstance(payload, dict) or not isinstance(fingerprint, str):
        raise SweepError(f"missing arm fingerprint for {pair[0]}/{pair[1]}")
    if canonical_sha256(payload) != fingerprint:
        raise SweepError(f"invalid arm fingerprint for {pair[0]}/{pair[1]}")
    expected_fields = {
        "catalog_sha256": input_hashes["catalog"],
        "orchestrator_sha256": input_hashes["runner"],
        "unialloc_implementation_sha256": input_hashes["unialloc_implementation"],
        "scenario_id": scenario_id,
        "archive_variant": pair[0],
        "allocator_variant": pair[1],
    }
    for field, expected in expected_fields.items():
        if payload.get(field) != expected:
            raise SweepError(
                f"arm fingerprint {field} mismatch for {pair[0]}/{pair[1]}"
            )


def validate_experiment_payload(
    payload: object,
    *,
    preflight_payload: object,
    scenario_id: str,
    variants: Sequence[str],
    archive_variants: Sequence[str],
    repetitions: int,
    input_hashes: dict[str, str],
    require_success: bool,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SweepError("experiment result must be a JSON object")
    if payload.get("scenario_id") != scenario_id:
        raise SweepError("experiment scenario id mismatch")
    if payload.get("repetitions_requested") != repetitions:
        raise SweepError("experiment repetition count mismatch")
    if payload.get("claim_grade") is not False:
        raise SweepError("experiment claim_grade must be false")
    if payload.get("unsafe_execution_requested") is not True:
        raise SweepError("experiment unsafe execution marker mismatch")
    if payload.get("variants") != list(variants):
        raise SweepError("experiment allocator variants mismatch")
    if payload.get("archive_variants") != list(archive_variants):
        raise SweepError("experiment archive variants mismatch")
    unexpected = payload.get("unexpected_arm_count")
    if (
        isinstance(unexpected, bool)
        or not isinstance(unexpected, int)
        or unexpected < 0
    ):
        raise SweepError("experiment unexpected arm count is invalid")
    if not isinstance(payload.get("orchestration_success"), bool):
        raise SweepError("experiment orchestration success marker is invalid")

    preflight = validate_preflight_payload(
        preflight_payload,
        scenario_id=scenario_id,
        variants=variants,
        archive_variants=archive_variants,
        repetitions=repetitions,
        input_hashes=input_hashes,
    )
    planned = validate_matrix_records(
        preflight["planned_arms"],
        variants=variants,
        archive_variants=archive_variants,
        label="preflight planned_arms",
    )
    arms = validate_matrix_records(
        payload.get("arms"),
        variants=variants,
        archive_variants=archive_variants,
        label="experiment arms",
    )
    for pair, arm in arms.items():
        if arm.get("scenario_id") != scenario_id:
            raise SweepError(f"arm scenario id mismatch for {pair[0]}/{pair[1]}")
        if arm.get("repetitions_requested") != repetitions:
            raise SweepError(f"arm repetition count mismatch for {pair[0]}/{pair[1]}")
        status = arm.get("execution_status")
        if not isinstance(status, str):
            raise SweepError(f"arm execution status missing for {pair[0]}/{pair[1]}")
        supported = planned[pair]["supported"]
        if supported and status in UNSUPPORTED_STATUSES:
            raise SweepError(
                f"supported arm reported unsupported for {pair[0]}/{pair[1]}"
            )
        if not supported and status not in UNSUPPORTED_STATUSES:
            raise SweepError(f"unsupported arm status mismatch for {pair[0]}/{pair[1]}")
        if (
            status == "completed"
            or "fingerprint" in arm
            or "fingerprint_payload" in arm
        ):
            validate_arm_fingerprint(
                arm,
                scenario_id=scenario_id,
                pair=pair,
                input_hashes=input_hashes,
            )
        if require_success and supported and status != "completed":
            raise SweepError(
                f"successful matrix has nonterminal arm {pair[0]}/{pair[1]}"
            )
    if require_success:
        if payload.get("orchestration_success") is not True or unexpected != 0:
            raise SweepError("experiment matrix did not complete successfully")
    return payload


def load_valid_result(
    *,
    stage: str,
    path: pathlib.Path,
    scenario_id: str,
    variants: Sequence[str],
    archive_variants: Sequence[str],
    repetitions: int,
    input_hashes: dict[str, str],
    require_success: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "runner did not produce a fresh valid result file"
    try:
        payload = load_json(path)
        if stage == "preflight":
            validated = validate_preflight_payload(
                payload,
                scenario_id=scenario_id,
                variants=variants,
                archive_variants=archive_variants,
                repetitions=repetitions,
                input_hashes=input_hashes,
            )
        else:
            preflight_path = path.parent / "preflight.json"
            if not preflight_path.is_file():
                raise SweepError("experiment result has no colocated preflight result")
            validated = validate_experiment_payload(
                payload,
                preflight_payload=load_json(preflight_path),
                scenario_id=scenario_id,
                variants=variants,
                archive_variants=archive_variants,
                repetitions=repetitions,
                input_hashes=input_hashes,
                require_success=require_success,
            )
    except (OSError, json.JSONDecodeError, SweepError) as error:
        return None, f"fresh valid result required: {error}"
    return validated, None


def matrix_binding_sha256(
    *, stage: str, payload: dict[str, Any], result_path: pathlib.Path
) -> str:
    if stage == "preflight":
        matrix: object = payload["planned_arms"]
    else:
        matrix = {
            "preflight_sha256": sha256_file(result_path.parent / "preflight.json"),
            "arms": [
                {
                    "archive_variant": arm.get("archive_variant"),
                    "allocator_variant": arm.get("allocator_variant"),
                    "execution_status": arm.get("execution_status"),
                    "fingerprint": arm.get("fingerprint"),
                }
                for arm in payload["arms"]
            ],
        }
    return canonical_sha256(matrix)


def write_result_binding(
    *,
    stage: str,
    result_path: pathlib.Path,
    payload: dict[str, Any],
    scenario_id: str,
    input_hashes: dict[str, str],
) -> dict[str, Any]:
    binding = {
        "schema_version": BINDING_SCHEMA,
        "stage": stage,
        "scenario_id": scenario_id,
        "result_sha256": sha256_file(result_path),
        "matrix_sha256": matrix_binding_sha256(
            stage=stage, payload=payload, result_path=result_path
        ),
        "catalog_sha256": input_hashes["catalog"],
        "runner_sha256": input_hashes["runner"],
        "script_sha256": input_hashes["script"],
        "unialloc_implementation_sha256": input_hashes["unialloc_implementation"],
    }
    write_json(result_binding_path(result_path), binding)
    return binding


def valid_existing_result(
    *,
    stage: str,
    path: pathlib.Path,
    scenario_id: str,
    variants: Sequence[str],
    archive_variants: Sequence[str],
    repetitions: int,
    input_hashes: dict[str, str],
) -> dict[str, Any] | None:
    payload, _reason = load_valid_result(
        stage=stage,
        path=path,
        scenario_id=scenario_id,
        variants=variants,
        archive_variants=archive_variants,
        repetitions=repetitions,
        input_hashes=input_hashes,
        require_success=True,
    )
    if payload is None:
        return None
    binding_path = result_binding_path(path)
    if not binding_path.is_file():
        return None
    try:
        binding = load_json(binding_path)
        expected = {
            "schema_version": BINDING_SCHEMA,
            "stage": stage,
            "scenario_id": scenario_id,
            "result_sha256": sha256_file(path),
            "matrix_sha256": matrix_binding_sha256(
                stage=stage, payload=payload, result_path=path
            ),
            "catalog_sha256": input_hashes["catalog"],
            "runner_sha256": input_hashes["runner"],
            "script_sha256": input_hashes["script"],
            "unialloc_implementation_sha256": input_hashes["unialloc_implementation"],
        }
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    if not isinstance(binding, dict) or any(
        binding.get(key) != value for key, value in expected.items()
    ):
        return None
    return payload


def empty_counts() -> dict[str, int]:
    return {key: 0 for key in COUNT_KEYS}


def count_from_preflight(payload: dict[str, Any]) -> dict[str, int]:
    arms = payload["planned_arms"]
    unsupported = sum(1 for arm in arms if not arm["supported"])
    supported = len(arms) - unsupported
    repetitions = int(payload["repetitions_requested"])
    return {
        "arm_count": len(arms),
        "terminal_arm_count": len(arms),
        "expected_compile_rejection_count": 0,
        "planned_repetition_slots": supported * repetitions,
        "executed_repetitions": 0,
        "run_count": 0,
        "unsupported_count": unsupported,
        "unexpected_count": 0,
    }


def arm_expected_compile_rejection(arm: dict[str, Any]) -> bool:
    oracle = arm.get("oracle_validation")
    contract = oracle.get("expected_contract") if isinstance(oracle, dict) else None
    return isinstance(contract, dict) and contract.get("kind") == "compile_rejection"


def count_from_experiment(payload: dict[str, Any]) -> dict[str, int]:
    arms = payload["arms"]
    executed = sum(
        int(arm.get("repetition_summary", {}).get("executed", 0))
        for arm in arms
        if isinstance(arm.get("repetition_summary"), dict)
    )
    unsupported = sum(
        1 for arm in arms if arm["execution_status"] in UNSUPPORTED_STATUSES
    )
    compile_rejections = sum(
        1
        for arm in arms
        if arm["execution_status"] not in UNSUPPORTED_STATUSES
        and arm_expected_compile_rejection(arm)
    )
    planned_slots = sum(
        int(arm["repetitions_requested"])
        for arm in arms
        if arm["execution_status"] not in UNSUPPORTED_STATUSES
        and not arm_expected_compile_rejection(arm)
    )

    def is_terminal(arm: dict[str, Any]) -> bool:
        status = arm["execution_status"]
        if status in UNSUPPORTED_STATUSES:
            return True
        if status != "completed":
            return False
        oracle = arm.get("oracle_validation")
        oracle_status = oracle.get("status") if isinstance(oracle, dict) else None
        if arm_expected_compile_rejection(arm):
            return oracle_status in OBSERVED_COMPILE_REJECTION_STATUSES
        summary = arm.get("repetition_summary")
        return isinstance(summary, dict) and summary.get("executed") == arm.get(
            "repetitions_requested"
        )

    return {
        "arm_count": len(arms),
        "terminal_arm_count": sum(1 for arm in arms if is_terminal(arm)),
        "expected_compile_rejection_count": compile_rejections,
        "planned_repetition_slots": planned_slots,
        "executed_repetitions": executed,
        "run_count": executed,
        "unsupported_count": unsupported,
        "unexpected_count": int(payload["unexpected_arm_count"]),
    }


def counts_for(stage: str, payload: dict[str, Any] | None) -> dict[str, int]:
    if payload is None:
        return empty_counts()
    return (
        count_from_preflight(payload)
        if stage == "preflight"
        else count_from_experiment(payload)
    )


def stage_command(
    *, stage: str, scenario_id: str, output_dir: pathlib.Path, args: argparse.Namespace
) -> list[str]:
    command = runner_command_base(args) + [
        "--action",
        stage,
        "--scenario",
        scenario_id,
        "--output-dir",
        str(output_dir),
    ]
    if args.allow_download:
        command.append("--allow-download")
    if stage == "run":
        command.append("--execute-unsafe")
    return command


def remove_stale_result(stage: str, result_path: pathlib.Path) -> None:
    for path in (result_path, result_binding_path(result_path)):
        if path.exists() or path.is_symlink():
            if not path.is_file() and not path.is_symlink():
                raise SweepError(f"expected result path is not a file: {path}")
            path.unlink()
    if stage == "run":
        preflight_path = result_path.parent / "preflight.json"
        preflight_binding = result_binding_path(preflight_path)
        for path in (preflight_path, preflight_binding):
            if path.exists() or path.is_symlink():
                if not path.is_file() and not path.is_symlink():
                    raise SweepError(f"expected preflight path is not a file: {path}")
                path.unlink()


def run_one(
    *,
    stage: str,
    scenario: dict[str, Any],
    args: argparse.Namespace,
    variants: Sequence[str],
    archive_variants: Sequence[str],
    input_hashes: dict[str, str],
) -> dict[str, Any]:
    scenario_id = scenario["scenario_id"]
    output_dir = scenario_output_dir(args.output_dir, scenario_id)
    result_path = expected_result_path(stage, output_dir)
    logs_dir = output_dir / "logs"
    stdout_path = logs_dir / f"{stage}.stdout.log"
    stderr_path = logs_dir / f"{stage}.stderr.log"
    command = stage_command(
        stage=stage, scenario_id=scenario_id, output_dir=output_dir, args=args
    )
    existing = (
        valid_existing_result(
            stage=stage,
            path=result_path,
            scenario_id=scenario_id,
            variants=variants,
            archive_variants=archive_variants,
            repetitions=args.repetitions,
            input_hashes=input_hashes,
        )
        if args.resume_successful
        else None
    )
    if existing is not None:
        return {
            "scenario_id": scenario_id,
            "case_id": scenario.get("case_id"),
            "crate": scenario.get("crate"),
            "tool": scenario.get("tool"),
            "stage": stage,
            "status": "resumed",
            "exit_code": 0,
            "command": command,
            "result_path": str(result_path.resolve()),
            "result_sha256": sha256_file(result_path),
            "stdout_log": str(stdout_path.resolve()),
            "stderr_log": str(stderr_path.resolve()),
            **counts_for(stage, existing),
        }

    logs_dir.mkdir(parents=True, exist_ok=True)
    remove_stale_result(stage, result_path)
    started = int(time.time())
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    payload, validation_error = load_valid_result(
        stage=stage,
        path=result_path,
        scenario_id=scenario_id,
        variants=variants,
        archive_variants=archive_variants,
        repetitions=args.repetitions,
        input_hashes=input_hashes,
        require_success=False,
    )
    payload_success = payload is not None and (
        stage == "preflight"
        or (
            payload.get("orchestration_success") is True
            and payload.get("unexpected_arm_count") == 0
        )
    )
    successful = completed.returncode == 0 and payload_success
    failure_reason = None
    if validation_error is not None:
        failure_reason = validation_error
    elif completed.returncode != 0:
        failure_reason = f"experiment runner exited with status {completed.returncode}"
    elif not payload_success:
        failure_reason = "fresh valid result reports an unsuccessful experiment matrix"

    result_sha = sha256_file(result_path) if payload is not None else None
    if successful and payload is not None:
        write_result_binding(
            stage=stage,
            result_path=result_path,
            payload=payload,
            scenario_id=scenario_id,
            input_hashes=input_hashes,
        )
    record = {
        "scenario_id": scenario_id,
        "case_id": scenario.get("case_id"),
        "crate": scenario.get("crate"),
        "tool": scenario.get("tool"),
        "stage": stage,
        "status": "success" if successful else "failed",
        "exit_code": completed.returncode,
        "command": command,
        "started_at_unix": started,
        "completed_at_unix": int(time.time()),
        "result_path": str(result_path.resolve()),
        "result_sha256": result_sha,
        "stdout_log": str(stdout_path.resolve()),
        "stderr_log": str(stderr_path.resolve()),
        **counts_for(stage, payload),
    }
    if failure_reason is not None:
        record["failure_reason"] = failure_reason
    return record


def failed_worker_record(
    *,
    stage: str,
    scenario: dict[str, Any],
    args: argparse.Namespace,
    error: BaseException,
) -> dict[str, Any]:
    scenario_id = scenario["scenario_id"]
    output_dir = scenario_output_dir(args.output_dir, scenario_id)
    logs_dir = output_dir / "logs"
    return {
        "scenario_id": scenario_id,
        "case_id": scenario.get("case_id"),
        "crate": scenario.get("crate"),
        "tool": scenario.get("tool"),
        "stage": stage,
        "status": "failed",
        "exit_code": None,
        "failure_reason": f"worker exception: {type(error).__name__}: {error}",
        "result_path": str(expected_result_path(stage, output_dir).resolve()),
        "result_sha256": None,
        "stdout_log": str((logs_dir / f"{stage}.stdout.log").resolve()),
        "stderr_log": str((logs_dir / f"{stage}.stderr.log").resolve()),
        **empty_counts(),
    }


def aggregate(records: Sequence[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "scenario_count": len(records),
        "success_count": sum(
            1 for record in records if record["status"] in {"success", "resumed"}
        ),
        "failed_count": sum(1 for record in records if record["status"] == "failed"),
        "resumed_count": sum(1 for record in records if record["status"] == "resumed"),
    }
    totals.update(
        {key: sum(int(record.get(key, 0)) for record in records) for key in COUNT_KEYS}
    )
    return totals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=DEFAULT_CATALOG)
    parser.add_argument("--action", choices=("preflight", "run"), default="preflight")
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--archive-variants", default=",".join(ARCHIVE_VARIANTS))
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--cache", type=pathlib.Path, required=True)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--execute-unsafe", action="store_true")
    parser.add_argument("--resume-successful", action="store_true")
    parser.add_argument("--scenario-jobs", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--build-timeout", type=int, default=900)
    parser.add_argument("--run-timeout", type=int, default=60)
    return parser


def is_same_or_ancestor(candidate: pathlib.Path, target: pathlib.Path) -> bool:
    return candidate == target or candidate in target.parents


def normalize_args(args: argparse.Namespace) -> None:
    args.catalog = args.catalog.expanduser().resolve()
    args.cache = args.cache.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    if args.action == "run" and not args.execute_unsafe:
        raise SweepError("run requires the explicit --execute-unsafe opt-in")
    if args.scenario_jobs <= 0 or args.jobs <= 0 or args.repetitions <= 0:
        raise SweepError("scenario-jobs, jobs, and repetitions must be positive")
    if args.build_timeout <= 0 or args.run_timeout <= 0:
        raise SweepError("timeouts must be positive")
    if args.allow_download and args.scenario_jobs > 1:
        raise SweepError("--allow-download requires --scenario-jobs 1 for cache safety")
    unsafe_targets = (
        ROOT.resolve(),
        pathlib.Path.home().resolve(),
        pathlib.Path.cwd().resolve(),
        args.catalog,
        args.cache,
        EXPERIMENT_RUNNER.resolve(),
    )
    filesystem_root = pathlib.Path(args.output_dir.anchor).resolve()
    if args.output_dir == filesystem_root or any(
        is_same_or_ancestor(args.output_dir, target) for target in unsafe_targets
    ):
        raise SweepError(
            "--output-dir must be a dedicated sweep directory, not a filesystem root "
            "or ancestor of repository, home, catalog, or cache inputs"
        )
    args.scenario = [validate_scenario_id(value) for value in args.scenario]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        normalize_args(args)
        variants = parse_csv(args.variants, VARIANTS, "allocator variants")
        archive_variants = parse_csv(
            args.archive_variants, ARCHIVE_VARIANTS, "archive variants"
        )
        input_hashes = capture_input_hashes(args)
        scenarios = load_scenarios(args)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.scenario_jobs
        ) as pool:
            futures = {
                pool.submit(
                    run_one,
                    stage=args.action,
                    scenario=scenario,
                    args=args,
                    variants=variants,
                    archive_variants=archive_variants,
                    input_hashes=input_hashes,
                ): scenario
                for scenario in scenarios
            }
            for future in concurrent.futures.as_completed(futures):
                scenario = futures[future]
                try:
                    records.append(future.result())
                except Exception as error:  # scenario isolation is intentional
                    records.append(
                        failed_worker_record(
                            stage=args.action,
                            scenario=scenario,
                            args=args,
                            error=error,
                        )
                    )
        records.sort(key=lambda record: record["scenario_id"])
        ending_hashes, input_drift = detect_input_drift(args, input_hashes)
        totals = aggregate(records)
        summary_valid = totals["failed_count"] == 0 and not input_drift
        summary = {
            "schema_version": SWEEP_SCHEMA,
            "source": "unialloc-rustsec-heap-sweep",
            "claim_grade": False,
            "valid": summary_valid,
            "created_at_unix": int(time.time()),
            "stage": args.action,
            "unsafe_execution_requested": bool(args.execute_unsafe),
            "resume_successful": bool(args.resume_successful),
            "scenario_jobs": args.scenario_jobs,
            "variants": list(variants),
            "archive_variants": list(archive_variants),
            "repetitions": args.repetitions,
            "input_hashes_at_start": input_hashes,
            "input_hashes_at_end": ending_hashes,
            "input_drift": input_drift,
            "catalog": {
                "path": str(args.catalog),
                "sha256": input_hashes["catalog"],
            },
            "runner": {
                "path": str(EXPERIMENT_RUNNER.resolve()),
                "sha256": input_hashes["runner"],
            },
            "script": {
                "path": str(pathlib.Path(__file__).resolve()),
                "sha256": input_hashes["script"],
            },
            "unialloc_implementation_sha256": input_hashes["unialloc_implementation"],
            "cache": str(args.cache),
            "output_dir": str(args.output_dir),
            "scenario_filters": list(args.scenario),
            "records": records,
            "totals": totals,
            "claim_boundary": (
                "This sweep is orchestration evidence only. Per-arm outcomes remain "
                "non-claim-grade until critical-site coverage and preregistered "
                "repetition criteria are validated."
            ),
        }
        summary["summary_sha256"] = canonical_sha256(
            {key: value for key, value in summary.items() if key != "summary_sha256"}
        )
        write_json(args.output_dir / "sweep-summary.json", summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary_valid else 1
    except (
        SweepError,
        OSError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
