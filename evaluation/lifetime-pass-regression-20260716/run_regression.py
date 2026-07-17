#!/usr/bin/env python3
"""Build and compare the compatibility and standalone lifetime rustc passes.

The runner is intentionally fail-closed:

* every invocation owns a new run directory;
* both target binaries are rebuilt from the same runner tree and allocator rlib;
* compiler audits and executable ``.text`` are parity gates before timing;
* every measurement is a fresh process with zero warm-up work; and
* operation time and operation-window peak RSS are reported separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_SCRIPTS = ROOT / "evaluation" / "scripts"
sys.path.insert(0, str(CAMPAIGN_SCRIPTS))
import lifetime_prior_six_program_campaign as campaign  # noqa: E402


DEFAULT_REFERENCE_RAW = Path(
    "/home/hanqing/alloc/UniAlloc-compiler-directed-swc-run-20260716/"
    "evaluation/raw/four-arm-other-targets-20260716"
)
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "raw"


@dataclass(frozen=True)
class Target:
    name: str
    package: str
    target_crate: str
    target_package: str
    binary_name: str
    prefix: str
    args: tuple[str, ...]
    fixed_fields: tuple[str, ...]
    expected_digest: str
    pre_extraction_binary_sha256: str
    pre_extraction_canonical_audit_sha256: str


TARGETS: dict[str, Target] = {
    "bedrock": Target(
        name="bedrock",
        package="unialloc-compiler-directed-bedrock",
        target_crate="bedrock_level",
        target_package="bedrock_level",
        binary_name="unialloc-compiler-directed-bedrock",
        prefix="UNIALLOC_COMPILER_DIRECTED_BEDROCK=",
        args=("1024", "3000000"),
        fixed_fields=(
            "correctness",
            "chunks",
            "iterations",
            "lookups",
            "digest",
        ),
        expected_digest="8ff01d6c0978f587",
        pre_extraction_binary_sha256=(
            "ed26bc99472fd3030394ae877f8a0ea7fdfddc75145f254b2c9d5e405ff12869"
        ),
        pre_extraction_canonical_audit_sha256=(
            "b1471e9e423946ed15f77df26a7965b240cda261e2f5f3256bb4e8d63308a126"
        ),
    ),
    "convex": Target(
        name="convex",
        package="unialloc-compiler-directed-convex-holiday",
        target_crate="convex_core",
        target_package="convex-core",
        binary_name="unialloc-compiler-directed-convex-holiday",
        prefix="UNIALLOC_COMPILER_DIRECTED_CONVEX=",
        args=("2731", "150000000"),
        fixed_fields=(
            "correctness",
            "calendar_count",
            "queries",
            "bitmap_bytes_per_calendar",
            "compiler_long_cohort_bytes",
            "date_count",
            "holidays_per_calendar",
            "holiday_hits",
            "business_hits",
            "digest",
        ),
        expected_digest="5b49db0f0b986115",
        pre_extraction_binary_sha256=(
            "55fa4a7eb764c87021c43c1b68e8ff070c84faedeba14569c64163ce7553e1f7"
        ),
        pre_extraction_canonical_audit_sha256=(
            "b04c9cc050b9dfe42104b61642d58e3969261659b44fcbab24d7bde05c7898af"
        ),
    ),
}


class RegressionError(RuntimeError):
    """A fail-closed build, parity, correctness, or performance failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def pass_source_closure(source: Path) -> dict[str, Any]:
    """Hash the entry point and shared implementation files it compiles."""
    candidates = [
        ("entrypoint", source.resolve()),
        (
            "shared_engine",
            source.parent.resolve() / "unialloc-rustc-driver-engine.rs",
        ),
        ("lifetime_policy", source.parent.resolve() / "lifetime_aware.rs"),
    ]
    files = []
    for role, path in candidates:
        if not path.is_file():
            raise RegressionError(f"pass source closure member is missing: {path}")
        try:
            stable_path = str(path.relative_to(ROOT.resolve()))
        except ValueError:
            stable_path = path.name
        files.append(
            {
                "role": role,
                "path": str(path),
                "stable_path": stable_path,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    hash_material = [
        {
            "role": row["role"],
            "stable_path": row["stable_path"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
        }
        for row in files
    ]
    return {
        "algorithm": (
            "sha256-canonical-json-over-roles-stable-paths-bytes-and-file-sha256-v1"
        ),
        "files": files,
        "hash_material": hash_material,
        "sha256": canonical_sha256(hash_material),
    }


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    artifact_prefix: Path,
) -> None:
    artifact_prefix.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    artifact_prefix.with_suffix(".stdout").write_bytes(completed.stdout)
    artifact_prefix.with_suffix(".stderr").write_bytes(completed.stderr)
    if completed.returncode:
        tail = completed.stderr.decode(errors="replace")[-20000:]
        raise RegressionError(
            f"command failed ({completed.returncode}): {command!r}\n{tail}"
        )


def create_run_dir(output_root: Path, run_id: str) -> Path:
    if not run_id or run_id in {".", ".."} or "/" in run_id:
        raise RegressionError("run id must be one nonempty path component")
    run_dir = output_root / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise RegressionError(
            f"refusing to reuse existing run directory: {run_dir}"
        ) from error
    return run_dir


def compile_pass(
    *, source: Path, output: Path, toolchain: str, label: str, run_dir: Path
) -> dict[str, Any]:
    if not source.is_file():
        raise RegressionError(f"missing {label} pass source: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    command = [
        "rustc",
        f"+{toolchain}",
        "--cfg",
        "unialloc_rustc_current",
        str(source.resolve()),
        "-O",
        "-o",
        str(output.resolve()),
    ]
    run_checked(
        command,
        cwd=ROOT,
        env=env,
        artifact_prefix=run_dir / "build" / "passes" / label,
    )
    closure = pass_source_closure(source)
    return {
        "label": label,
        "source": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "source_closure": closure,
        "source_closure_sha256": closure["sha256"],
        "binary": str(output.resolve()),
        "binary_sha256": sha256_file(output),
        "command": command,
    }


def only_text_sha256(binary: Path, scratch: Path) -> dict[str, Any]:
    objcopy = shutil.which("objcopy")
    if objcopy is None:
        raise RegressionError("objcopy is required for the executable .text parity gate")
    scratch.parent.mkdir(parents=True, exist_ok=True)
    if scratch.exists():
        scratch.unlink()
    binary_sha256_before = sha256_file(binary)
    completed = subprocess.run(
        [
            objcopy,
            "--only-section=.text",
            "--output-target=binary",
            str(binary),
            str(scratch),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode or not scratch.is_file():
        raise RegressionError(
            f"failed to copy .text from {binary}: "
            + completed.stderr.decode(errors="replace")[-4000:]
        )
    binary_sha256_after = sha256_file(binary)
    if binary_sha256_after != binary_sha256_before:
        raise RegressionError(
            f".text extraction mutated {binary}: "
            f"{binary_sha256_before} -> {binary_sha256_after}"
        )
    result = {
        "binary": str(binary.resolve()),
        "binary_sha256": binary_sha256_before,
        "binary_unchanged_by_text_extraction": True,
        "text_bytes": scratch.stat().st_size,
        "text_sha256": sha256_file(scratch),
    }
    scratch.unlink()
    return result


def _candidate_is_lifetime_relevant(candidate: Mapping[str, Any]) -> bool:
    return candidate.get("lifetime_hint_basis") != "not_applicable_skipped_candidate"


def canonical_lifetime_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    features = candidate.get("lifetime_analysis_features")
    if not isinstance(features, Mapping):
        features = {}
    runtime_key = features.get("runtime_join_key")
    if not isinstance(runtime_key, Mapping):
        runtime_key = {}
    return {
        "key": {
            "allocation_site_id": candidate.get("allocation_site_id"),
            "callsite": candidate.get("callsite"),
            "type_id": candidate.get("type_id"),
            "module_id": candidate.get("module_id"),
            "mir_function": candidate.get("mir_function"),
            "basic_block": candidate.get("basic_block"),
            "destination_place": candidate.get("destination_place"),
            "lowering_kind": candidate.get("lowering_kind"),
        },
        "decision": {
            "lifetime_hint": candidate.get("lifetime_hint"),
            "lifetime_hint_confidence": candidate.get(
                "lifetime_hint_confidence"
            ),
            "lifetime_hint_basis": candidate.get("lifetime_hint_basis"),
            "placement_hint": candidate.get("placement_hint"),
            "placement_hint_basis": candidate.get("placement_hint_basis"),
        },
        "layout": {
            "size_operand": candidate.get("size_operand"),
            "align_operand": candidate.get("align_operand"),
            "requested_layout_basis": features.get("requested_layout_basis"),
            "runtime_layout_captured_by_semantic_scope": features.get(
                "runtime_layout_captured_by_semantic_scope"
            ),
            "runtime_observation_min_requested_bytes": features.get(
                "runtime_observation_min_requested_bytes"
            ),
            "runtime_observation_max_requested_bytes": features.get(
                "runtime_observation_max_requested_bytes"
            ),
            "runtime_join_requested_size_bytes": runtime_key.get(
                "requested_size_bytes"
            ),
            "runtime_join_requested_align_bytes": runtime_key.get(
                "requested_align_bytes"
            ),
        },
        "lowering": {
            "rewrite_status": candidate.get("rewrite_status"),
            "replacement_symbol": candidate.get("replacement_symbol"),
            "replacement_resolution_status": candidate.get(
                "replacement_resolution_status"
            ),
            "metadata_pairing_contract": candidate.get(
                "metadata_pairing_contract"
            ),
            "semantic_scope_unwind_pop_inserted": candidate.get(
                "semantic_scope_unwind_pop_inserted"
            ),
        },
    }


def canonical_lifetime_audit(audit_dir: Path) -> dict[str, Any]:
    paths = sorted(audit_dir.glob("*.json"))
    if not paths:
        raise RegressionError(f"audit directory contains no JSON files: {audit_dir}")
    all_candidates = []
    for path in paths:
        payload = json.loads(path.read_text())
        candidates = payload.get("rewrite_candidates")
        if not isinstance(candidates, list):
            raise RegressionError(f"audit has no rewrite_candidates list: {path}")
        canonical_candidates = [
            canonical_lifetime_candidate(candidate)
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and _candidate_is_lifetime_relevant(candidate)
        ]
        all_candidates.extend(canonical_candidates)
    all_candidates.sort(
        key=lambda row: json.dumps(
            row["key"], sort_keys=True, separators=(",", ":")
        )
    )
    return {
        "schema_version": 1,
        "audit_file_count": len(paths),
        "lifetime_candidate_count": len(all_candidates),
        "candidates": all_candidates,
    }


def pre_extraction_reference(
    *, reference_raw: Path, target: Target
) -> dict[str, Any]:
    build_path = reference_raw / "builds" / target.name / "build.json"
    audit_dir = reference_raw / "builds" / target.name / "audit"
    if not build_path.is_file():
        raise RegressionError(f"missing pre-extraction build record: {build_path}")
    build = json.loads(build_path.read_text())
    build_record_binary_sha256 = build.get("binary_sha256")
    if (
        not isinstance(build_record_binary_sha256, str)
        or len(build_record_binary_sha256) != 64
    ):
        raise RegressionError(
            f"pre-extraction build record has no valid binary SHA-256: {build_path}"
        )
    if build_record_binary_sha256 != target.pre_extraction_binary_sha256:
        raise RegressionError(
            f"pre-extraction {target.name} build record disagrees with the "
            f"checked-in binary pin: {build_record_binary_sha256} != "
            f"{target.pre_extraction_binary_sha256}"
        )
    reference_binary = reference_raw / "bin" / target.name
    if not reference_binary.is_file():
        raise RegressionError(f"missing pre-extraction target binary: {reference_binary}")
    observed_reference_binary_sha256 = sha256_file(reference_binary)
    if observed_reference_binary_sha256 != target.pre_extraction_binary_sha256:
        raise RegressionError(
            f"pre-extraction {target.name} binary disagrees with its checked-in "
            f"pin: {observed_reference_binary_sha256} != "
            f"{target.pre_extraction_binary_sha256}"
        )
    audit = canonical_lifetime_audit(audit_dir)
    canonical_audit_sha256 = canonical_sha256(audit)
    if canonical_audit_sha256 != target.pre_extraction_canonical_audit_sha256:
        raise RegressionError(
            f"pre-extraction {target.name} audit disagrees with its checked-in "
            f"pin: {canonical_audit_sha256} != "
            f"{target.pre_extraction_canonical_audit_sha256}"
        )
    return {
        "source": "immutable-pre-extraction-four-arm-build-v1",
        "build_record": str(build_path.resolve()),
        "build_record_sha256": sha256_file(build_path),
        "binary": str(reference_binary.resolve()),
        "checked_in_binary_sha256": target.pre_extraction_binary_sha256,
        "build_record_binary_sha256": build_record_binary_sha256,
        "expected_binary_sha256": target.pre_extraction_binary_sha256,
        "observed_reference_binary_sha256": observed_reference_binary_sha256,
        "reference_binary_matches_build_record": True,
        "audit_dir": str(audit_dir.resolve()),
        "checked_in_canonical_audit_sha256": (
            target.pre_extraction_canonical_audit_sha256
        ),
        "canonical_audit_sha256": canonical_audit_sha256,
        "lifetime_candidate_count": audit["lifetime_candidate_count"],
        "canonical_audit": audit,
    }


def parity_record(
    *,
    target: str,
    compat_binary: Path,
    standalone_binary: Path,
    compat_audit: Path,
    standalone_audit: Path,
    reference: Mapping[str, Any],
    scratch_dir: Path,
) -> dict[str, Any]:
    compat_code = only_text_sha256(compat_binary, scratch_dir / "compat.text")
    standalone_code = only_text_sha256(
        standalone_binary, scratch_dir / "standalone.text"
    )
    code_equal = (
        compat_code["text_sha256"] == standalone_code["text_sha256"]
        and compat_code["text_bytes"] == standalone_code["text_bytes"]
    )
    compat_canonical = canonical_lifetime_audit(compat_audit)
    standalone_canonical = canonical_lifetime_audit(standalone_audit)
    audit_equal = compat_canonical == standalone_canonical
    expected_binary_sha256 = reference["expected_binary_sha256"]
    reference_binary_equal = (
        compat_code["binary_sha256"] == expected_binary_sha256
        and standalone_code["binary_sha256"] == expected_binary_sha256
    )
    reference_audit_sha256 = reference["canonical_audit_sha256"]
    reference_audit_equal = (
        canonical_sha256(compat_canonical) == reference_audit_sha256
        and canonical_sha256(standalone_canonical) == reference_audit_sha256
    )
    record = {
        "schema_version": 1,
        "target": target,
        "passed": (
            code_equal
            and audit_equal
            and reference_binary_equal
            and reference_audit_equal
        ),
        "pre_extraction_reference": {
            "passed": reference_binary_equal and reference_audit_equal,
            "source": reference["source"],
            "build_record": reference["build_record"],
            "build_record_sha256": reference["build_record_sha256"],
            "expected_binary_sha256": expected_binary_sha256,
            "checked_in_binary_sha256": reference[
                "checked_in_binary_sha256"
            ],
            "build_record_binary_sha256": reference[
                "build_record_binary_sha256"
            ],
            "observed_reference_binary_sha256": reference[
                "observed_reference_binary_sha256"
            ],
            "compat_binary_sha256": compat_code["binary_sha256"],
            "standalone_binary_sha256": standalone_code["binary_sha256"],
            "binary_match_passed": reference_binary_equal,
            "expected_canonical_audit_sha256": reference_audit_sha256,
            "checked_in_canonical_audit_sha256": reference[
                "checked_in_canonical_audit_sha256"
            ],
            "compat_canonical_audit_sha256": canonical_sha256(compat_canonical),
            "standalone_canonical_audit_sha256": canonical_sha256(
                standalone_canonical
            ),
            "expected_lifetime_candidate_count": reference[
                "lifetime_candidate_count"
            ],
            "compat_lifetime_candidate_count": compat_canonical[
                "lifetime_candidate_count"
            ],
            "standalone_lifetime_candidate_count": standalone_canonical[
                "lifetime_candidate_count"
            ],
            "audit_match_passed": reference_audit_equal,
        },
        "code_parity": {
            "passed": code_equal,
            "compat": compat_code,
            "standalone": standalone_code,
        },
        "audit_parity": {
            "passed": audit_equal,
            "compat_canonical_sha256": canonical_sha256(compat_canonical),
            "standalone_canonical_sha256": canonical_sha256(
                standalone_canonical
            ),
            "compat_lifetime_candidate_count": compat_canonical[
                "lifetime_candidate_count"
            ],
            "standalone_lifetime_candidate_count": standalone_canonical[
                "lifetime_candidate_count"
            ],
        },
        "canonical_compat_audit": compat_canonical,
        "canonical_standalone_audit": standalone_canonical,
    }
    return record


def _copy_runner(reference_raw: Path, target: Target, work_dir: Path) -> Path:
    source = reference_raw / "builds" / target.name / "runner"
    if not source.is_dir():
        raise RegressionError(f"missing pinned runner tree: {source}")
    runner = work_dir / "runner"
    shutil.copytree(source, runner, ignore=shutil.ignore_patterns("target"))
    return runner


def _fresh_directories(paths: Iterable[Path]) -> None:
    for path in paths:
        if path.exists():
            raise RegressionError(f"refusing pre-existing build artifact: {path}")
        path.mkdir(parents=True)


def build_target_pair(
    *,
    target: Target,
    reference_raw: Path,
    reference: Mapping[str, Any],
    pass_binaries: Mapping[str, Path],
    run_dir: Path,
    jobs: int,
) -> dict[str, Any]:
    work = run_dir / "build" / "targets" / target.name
    work.mkdir(parents=True)
    runner = _copy_runner(reference_raw, target, work)
    target_dir = work / "target"
    temporary = work / "tmp"
    bin_dir = run_dir / "bin" / target.name
    _fresh_directories((target_dir, temporary, bin_dir))

    replacement_wrapper = reference_raw / "tools" / "replace-unialloc-source-wrapper"
    force_source = reference_raw / "builds" / target.name / "tools" / "force-wrapper"
    if not replacement_wrapper.is_file() or not force_source.is_file():
        raise RegressionError(f"missing pinned wrappers under {reference_raw}")
    force_wrapper = work / "force-wrapper"
    shutil.copy2(force_source, force_wrapper)
    force_wrapper.chmod(0o755)

    bootstrap_env = os.environ.copy()
    for name in (
        "RUSTC_WORKSPACE_WRAPPER",
        "UNIALLOC_AUTO_RUST_LIFETIME_PRIOR",
        "UNIALLOC_REWRITE_AUDIT_DIR",
        "UNIALLOC_PASS_LOG_DIR",
    ):
        bootstrap_env.pop(name, None)
    bootstrap_env.update(
        {
            "RUSTC_WRAPPER": str(replacement_wrapper.resolve()),
            "CARGO_TARGET_DIR": str(target_dir.resolve()),
            "CARGO_INCREMENTAL": "0",
            "CARGO_PROFILE_RELEASE_STRIP": "false",
            "RUSTFLAGS": "--cfg unialloc_lifetime_prior_compiler_prior",
            "TMPDIR": str(temporary.resolve()),
        }
    )
    run_checked(
        [
            "cargo",
            f"+{campaign.TOOLCHAIN}",
            "build",
            "--release",
            "--locked",
            "--jobs",
            str(jobs),
        ],
        cwd=runner,
        env=bootstrap_env,
        artifact_prefix=work / "bootstrap",
    )
    rlibs = sorted(
        (target_dir / "release" / "deps").glob("libunialloc-*.rlib"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if not rlibs:
        raise RegressionError(f"bootstrap produced no UniAlloc rlib for {target.name}")
    allocator_rlib = rlibs[-1]

    variants: dict[str, Any] = {}
    for variant in ("compat", "standalone"):
        run_checked(
            [
                "cargo",
                f"+{campaign.TOOLCHAIN}",
                "clean",
                "--release",
                "-p",
                target.target_package,
                "-p",
                target.package,
            ],
            cwd=runner,
            env=bootstrap_env,
            artifact_prefix=work / f"clean-{variant}",
        )
        audit_dir = work / "audits" / variant
        log_dir = work / "logs" / variant
        variant_tmp = work / "tmp-variants" / variant
        _fresh_directories((audit_dir, log_dir, variant_tmp))
        env = campaign._compiler_build_environment(
            "compiler-prior",
            wrapper=force_wrapper,
            audit_dir=audit_dir,
            pass_log_dir=log_dir,
            target_dir=target_dir,
            target_crates=(target.target_crate,),
            temporary_dir=variant_tmp,
        )
        env.update(
            {
                "UNIALLOC_FORCE_LOAD_CRATES": target.target_crate,
                "UNIALLOC_FORCE_LOAD_DRIVER": str(pass_binaries[variant].resolve()),
                "UNIALLOC_FORCE_LOAD_RLIB": str(allocator_rlib.resolve()),
                "UNIALLOC_FORCE_LOAD_DEPENDENCY_DIR": str(
                    allocator_rlib.parent.resolve()
                ),
                "UNIALLOC_ACTUAL_MIR_REWRITE": "0",
                "CARGO_PROFILE_RELEASE_STRIP": "false",
            }
        )
        run_checked(
            [
                "cargo",
                f"+{campaign.TOOLCHAIN}",
                "build",
                "--release",
                "--locked",
                "--message-format=json-render-diagnostics",
                "--jobs",
                str(jobs),
            ],
            cwd=runner,
            env=env,
            artifact_prefix=work / f"instrument-{variant}",
        )
        source_binary = target_dir / "release" / target.binary_name
        if not source_binary.is_file():
            raise RegressionError(f"missing built binary: {source_binary}")
        binary = bin_dir / variant
        shutil.copy2(source_binary, binary)
        variants[variant] = {
            "pass_binary": str(pass_binaries[variant].resolve()),
            "binary": str(binary.resolve()),
            "binary_sha256": sha256_file(binary),
            "audit_dir": str(audit_dir.resolve()),
            "audit_file_count": len(list(audit_dir.glob("*.json"))),
        }

    parity = parity_record(
        target=target.name,
        compat_binary=Path(variants["compat"]["binary"]),
        standalone_binary=Path(variants["standalone"]["binary"]),
        compat_audit=Path(variants["compat"]["audit_dir"]),
        standalone_audit=Path(variants["standalone"]["audit_dir"]),
        reference=reference,
        scratch_dir=work / "parity-scratch",
    )
    write_json(work / "parity.json", parity)
    if not parity["passed"]:
        raise RegressionError(
            f"{target.name}: pre-extraction reference, compiler audit, or .text "
            "parity gate failed; "
            f"see {work / 'parity.json'}"
        )
    record = {
        "schema_version": 1,
        "target": target.name,
        "runner": str(runner.resolve()),
        "allocator_rlib": str(allocator_rlib.resolve()),
        "allocator_rlib_sha256": sha256_file(allocator_rlib),
        "variants": variants,
        "parity_path": str((work / "parity.json").resolve()),
        "parity_passed": True,
        "pre_extraction_reference_gate": parity["pre_extraction_reference"],
    }
    write_json(work / "build.json", record)
    return record


def parse_prefixed_json(text: str, prefix: str) -> dict[str, Any]:
    rows = []
    for line in text.splitlines():
        if line.startswith(prefix):
            value = json.loads(line[len(prefix) :])
            if isinstance(value, dict):
                rows.append(value)
    if len(rows) != 1:
        raise RegressionError(
            f"expected exactly one fixed-work row with prefix {prefix!r}, got {len(rows)}"
        )
    return rows[0]


def resolve_cpu(cpu: str) -> int | None:
    if cpu == "none" or shutil.which("taskset") is None:
        return None
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    if cpu == "auto":
        return affinity[-1] if affinity else None
    try:
        selected = int(cpu)
    except ValueError as error:
        raise RegressionError(f"invalid CPU selector: {cpu}") from error
    if affinity and selected not in affinity:
        raise RegressionError(f"CPU {selected} is outside process affinity {affinity}")
    return selected


def fixed_projection(target: Target, row: Mapping[str, Any]) -> dict[str, Any]:
    missing = [field for field in target.fixed_fields if field not in row]
    if missing:
        raise RegressionError(f"{target.name}: fixed-work row misses {missing}")
    result = {field: row[field] for field in target.fixed_fields}
    if result["correctness"] is not True:
        raise RegressionError(f"{target.name}: correctness oracle failed")
    if result["digest"] != target.expected_digest:
        raise RegressionError(
            f"{target.name}: digest {result['digest']} != {target.expected_digest}"
        )
    return result


def measure_one(
    *,
    target: Target,
    variant: str,
    binary: Path,
    artifact_dir: Path,
    cpu: int | None,
    timeout: float,
    sample_interval: float,
) -> dict[str, Any]:
    if artifact_dir.exists():
        raise RegressionError(f"refusing to reuse measurement cell: {artifact_dir}")
    env = os.environ.copy()
    env.update(
        {
            "UNIALLOC_LIFETIME_EXPERIMENT_ARM": (
                "compiler-directed-hugepage-compiler-prior"
            ),
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
        }
    )
    command = [str(binary.resolve()), *target.args]
    if cpu is not None:
        command = ["taskset", "-c", str(cpu), *command]
    process = campaign.execute_monitored_process(
        command,
        cwd=ROOT,
        env=env,
        artifact_dir=artifact_dir,
        timeout=timeout,
        sample_interval=sample_interval,
    )
    stdout = Path(process["stdout_path"]).read_text(errors="replace")
    stderr = Path(process["stderr_path"]).read_text(errors="replace")
    if process["timed_out"] or process["exit_code"] != 0:
        raise RegressionError(
            f"{target.name}/{variant}: workload failed\n{stderr[-10000:]}"
        )
    if process["single_process_guard"].get("passed") is not True:
        raise RegressionError(f"{target.name}/{variant}: observed descendant process")
    fixed = parse_prefixed_json(stdout, target.prefix)
    projection = fixed_projection(target, fixed)
    operation_seconds = float(fixed["operation_seconds"])
    if operation_seconds <= 0:
        raise RegressionError(f"{target.name}/{variant}: nonpositive operation time")
    wall = float(process["wall_seconds"])
    lower = max(0.0, wall - operation_seconds - 0.10)
    upper = wall + 0.01
    samples = [
        sample
        for sample in process["smaps_samples"]
        if lower <= float(sample["elapsed_seconds"]) <= upper
    ]
    if not samples:
        samples = process["smaps_samples"][-min(3, len(process["smaps_samples"])) :]
    if not samples:
        raise RegressionError(f"{target.name}/{variant}: no procfs samples")
    operation_procfs = campaign.summarize_proc_samples(samples)
    full_procfs = campaign.summarize_proc_samples(process["smaps_samples"])
    stats = campaign.runtime_lifetime.parse_runtime_stats(stderr)
    if stats is None or int(stats.get("policy", -1)) != 9:
        raise RegressionError(f"{target.name}/{variant}: expected runtime policy 9")
    if int(stats.get("routed_allocations", 0)) <= 0:
        raise RegressionError(f"{target.name}/{variant}: no compiler-directed routes")
    raw_process = {key: value for key, value in process.items() if key != "smaps_samples"}
    record = {
        **raw_process,
        "schema_version": 1,
        "target": target.name,
        "variant": variant,
        "fresh_process": True,
        "warmup_iterations": 0,
        "cpu_affinity": [cpu] if cpu is not None else None,
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "fixed_work": fixed,
        "fixed_projection": projection,
        "operation_seconds": operation_seconds,
        "operation_window_bounds": {"lower": lower, "upper": upper},
        "operation_procfs": operation_procfs,
        "full_process_procfs": full_procfs,
        "runtime_policy": int(stats["policy"]),
        "routed_allocations": int(stats["routed_allocations"]),
    }
    write_json(artifact_dir / "operation-smaps-samples.json", samples)
    write_json(artifact_dir / "run.json", record)
    return record


def paired_effect(compat: Mapping[str, Any], standalone: Mapping[str, Any]) -> dict[str, Any]:
    compat_operation = float(compat["operation_seconds"])
    standalone_operation = float(standalone["operation_seconds"])
    compat_rss = int(compat["operation_procfs"]["peak_rss_kib"])
    standalone_rss = int(standalone["operation_procfs"]["peak_rss_kib"])
    return {
        "operation_slowdown_percent": (
            (standalone_operation / compat_operation) - 1.0
        )
        * 100.0,
        "peak_rss_increase_kib": standalone_rss - compat_rss,
        "peak_rss_increase_percent": ((standalone_rss / compat_rss) - 1.0) * 100.0,
        "compat_operation_seconds": compat_operation,
        "standalone_operation_seconds": standalone_operation,
        "compat_peak_rss_kib": compat_rss,
        "standalone_peak_rss_kib": standalone_rss,
    }


def measure_target(
    *,
    target: Target,
    build: Mapping[str, Any],
    run_dir: Path,
    pairs: int,
    cpu: int | None,
    timeout: float,
    sample_interval: float,
) -> dict[str, Any]:
    if pairs < 3:
        raise RegressionError("at least three AB+BA pairs are required")
    variants = build["variants"]
    binaries = {
        variant: Path(variants[variant]["binary"]) for variant in ("compat", "standalone")
    }
    runs = []
    effects = []
    expected_projection: dict[str, Any] | None = None
    for pair_index in range(1, pairs + 1):
        for order_name, order in (
            ("ab", ("compat", "standalone")),
            ("ba", ("standalone", "compat")),
        ):
            by_variant = {}
            for position, variant in enumerate(order, 1):
                artifact = (
                    run_dir
                    / "measure"
                    / target.name
                    / f"pair-{pair_index:02d}"
                    / order_name
                    / f"{position:02d}-{variant}"
                )
                row = measure_one(
                    target=target,
                    variant=variant,
                    binary=binaries[variant],
                    artifact_dir=artifact,
                    cpu=cpu,
                    timeout=timeout,
                    sample_interval=sample_interval,
                )
                row["pair_index"] = pair_index
                row["order"] = order_name
                row["position"] = position
                projection = row["fixed_projection"]
                if expected_projection is None:
                    expected_projection = projection
                elif projection != expected_projection:
                    raise RegressionError(
                        f"{target.name}: fixed work differs across fresh processes"
                    )
                runs.append(row)
                by_variant[variant] = row
            effect = paired_effect(by_variant["compat"], by_variant["standalone"])
            effect.update({"pair_index": pair_index, "order": order_name})
            effects.append(effect)

    operation_slowdown = statistics.median(
        effect["operation_slowdown_percent"] for effect in effects
    )
    rss_increase_kib = statistics.median(
        effect["peak_rss_increase_kib"] for effect in effects
    )
    compat_rss = statistics.median(
        effect["compat_peak_rss_kib"] for effect in effects
    )
    rss_allowance_kib = max(0.02 * compat_rss, 1024.0)
    operation_passed = operation_slowdown <= 2.0
    rss_passed = rss_increase_kib <= rss_allowance_kib
    result = {
        "schema_version": 1,
        "target": target.name,
        "passed": operation_passed and rss_passed,
        "fresh_process": True,
        "warmup_iterations": 0,
        "pair_count": pairs,
        "order_count": pairs * 2,
        "process_count": pairs * 4,
        "orders_per_pair": ["ab", "ba"],
        "cpu_affinity": [cpu] if cpu is not None else None,
        "fixed_work": expected_projection,
        "pre_extraction_reference_gate": build[
            "pre_extraction_reference_gate"
        ],
        "runs": runs,
        "paired_effects": effects,
        "operation_gate": {
            "passed": operation_passed,
            "median_slowdown_percent": operation_slowdown,
            "maximum_allowed_slowdown_percent": 2.0,
        },
        "peak_rss_gate": {
            "passed": rss_passed,
            "median_increase_kib": rss_increase_kib,
            "compat_median_peak_rss_kib": compat_rss,
            "maximum_allowed_increase_kib": rss_allowance_kib,
            "rule": "max(2% of compatibility median peak RSS, 1024 KiB)",
        },
    }
    write_json(run_dir / "results" / f"{target.name}.json", result)
    return result


def selected_targets(values: Sequence[str]) -> list[Target]:
    result = []
    for name in values:
        if name not in TARGETS:
            raise RegressionError(f"unknown target {name!r}; choose from {sorted(TARGETS)}")
        result.append(TARGETS[name])
    return result


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--reference-raw", type=Path, default=DEFAULT_REFERENCE_RAW)
    parser.add_argument(
        "--compat-source",
        type=Path,
        default=ROOT
        / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs",
    )
    parser.add_argument("--standalone-source", type=Path, required=True)
    parser.add_argument("--targets", nargs="+", default=["bedrock", "convex"])
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--cpu", default="auto", help="auto, none, or a CPU number")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--sample-interval", type=float, default=0.05)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_arguments(argv)
    targets = selected_targets(args.targets)
    if args.pairs < 3:
        raise RegressionError("--pairs must be at least 3")
    run_dir = create_run_dir(args.output_root.resolve(), args.run_id)
    cpu = resolve_cpu(args.cpu)
    # Resolve every immutable reference before compiling either pass or target.
    # A corrupted pre-extraction anchor leaves no build or measurement output.
    references = {
        target.name: pre_extraction_reference(
            reference_raw=args.reference_raw.resolve(), target=target
        )
        for target in targets
    }
    write_json(
        run_dir / "pre-extraction-references.json",
        {
            name: {
                key: value
                for key, value in reference.items()
                if key != "canonical_audit"
            }
            for name, reference in references.items()
        },
    )
    tools = run_dir / "tools"
    passes = {
        "compat": tools / "unialloc-rustc-compat",
        "standalone": tools / "unialloc-rustc-lifetime",
    }
    pass_builds = {
        "compat": compile_pass(
            source=args.compat_source.resolve(),
            output=passes["compat"],
            toolchain=campaign.TOOLCHAIN,
            label="compat",
            run_dir=run_dir,
        ),
        "standalone": compile_pass(
            source=args.standalone_source.resolve(),
            output=passes["standalone"],
            toolchain=campaign.TOOLCHAIN,
            label="standalone",
            run_dir=run_dir,
        ),
    }
    write_json(run_dir / "build" / "passes.json", pass_builds)
    builds: dict[str, Any] = {}
    results: dict[str, Any] = {}
    for target in targets:
        build = build_target_pair(
            target=target,
            reference_raw=args.reference_raw.resolve(),
            reference=references[target.name],
            pass_binaries=passes,
            run_dir=run_dir,
            jobs=args.jobs,
        )
        builds[target.name] = build
        results[target.name] = measure_target(
            target=target,
            build=build,
            run_dir=run_dir,
            pairs=args.pairs,
            cpu=cpu,
            timeout=args.timeout,
            sample_interval=args.sample_interval,
        )
    passed = all(result["passed"] for result in results.values())
    summary = {
        "schema_version": 1,
        "passed": passed,
        "run_id": args.run_id,
        "run_dir": str(run_dir.resolve()),
        "reference_raw": str(args.reference_raw.resolve()),
        "reference_raw_is_external_immutable_input": True,
        "fresh_run_directory": True,
        "warmup_iterations": 0,
        "cpu_affinity": [cpu] if cpu is not None else None,
        "pass_builds": pass_builds,
        "builds": builds,
        "results": {
            name: {
                "passed": result["passed"],
                "operation_gate": result["operation_gate"],
                "peak_rss_gate": result["peak_rss_gate"],
                "pre_extraction_reference_gate": result[
                    "pre_extraction_reference_gate"
                ],
                "result_path": str(
                    (run_dir / "results" / f"{name}.json").resolve()
                ),
            }
            for name, result in results.items()
        },
    }
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    if not passed:
        raise RegressionError(f"regression gate failed; see {run_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RegressionError as error:
        print(f"lifetime pass regression: {error}", file=sys.stderr)
        raise SystemExit(2)
