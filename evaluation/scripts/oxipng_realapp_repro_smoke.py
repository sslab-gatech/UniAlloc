#!/usr/bin/env python3
"""Reproduce the bounded Oxipng UniAlloc MIR/type-isolation functional smoke.

This is a functional/diagnostic gate only.  It copies a pinned Oxipng checkout to
an isolated temporary directory, applies the minimal UniAlloc allocator/stats
instrumentation, builds the real ``oxipng`` package through the UniAlloc MIR
rewrite rustc wrapper, runs one PNG optimization, and fails closed unless the
runtime and compiler-audit evidence matches the narrow contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_TOOLCHAIN = "nightly-2022-07-01"
DEFAULT_TARGET_CRATE = "oxipng"
DEFAULT_INPUT = pathlib.Path("tests/files/filter_0_for_palette_1.png")
DEFAULT_EXPECTED_OUTPUT_SHA256 = "565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff"
DEFAULT_PINNED_CHECKOUT = ROOT / "evaluation" / "external" / "_checkouts" / "external-R-Oxipng-Oxipng"
PASS_SOURCE = ROOT / "tools" / "unialloc-rustc-pass" / "unialloc-rustc-mir-rewrite-dry-run.rs"
STATS_PREFIX = "UNIALLOC_STATS_JSON="
SCOPED_STATUS_PATHS = [
    pathlib.Path("Cargo.toml"),
    pathlib.Path("Cargo.lock"),
    pathlib.Path("unialloc/src"),
    pathlib.Path("unialloc/Cargo.toml"),
    pathlib.Path("unialloc/build.rs"),
    pathlib.Path("alloc_macros"),
    PASS_SOURCE.relative_to(ROOT),
    pathlib.Path("evaluation/scripts/oxipng_realapp_repro_smoke.py"),
    pathlib.Path("evaluation/scripts/test_oxipng_realapp_repro_smoke.py"),
]

SEMANTIC_AND_BODY_AUDIT_FLAGS = (
    "actual_semantic_scope_rewrite",
    "body_clone_returned_to_rustc",
)
DIRECT_REWRITE_AUDIT_FLAGS = (
    "actual_allocator_call_replacement",
    "direct_local_metadata_abi",
    "direct_local_size_align_with_semantic_drop",
)
REQUIRED_AUDIT_FLAGS = SEMANTIC_AND_BODY_AUDIT_FLAGS + DIRECT_REWRITE_AUDIT_FLAGS
NO_SUPPORTED_DIRECT_REWRITE_STATUS = "no_supported_rewrite_candidates"


class SmokeError(RuntimeError):
    """Expected fail-closed smoke failure."""


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    args: Iterable[str | os.PathLike[str]],
    *,
    cwd: pathlib.Path,
    env: dict[str, str] | None = None,
    timeout: int,
    check: bool = False,
) -> CommandResult:
    argv = [str(arg) for arg in args]
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    result = CommandResult(argv, proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise SmokeError(
            f"command failed ({proc.returncode}): {' '.join(argv)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return result


def git_output(repo: pathlib.Path, args: list[str], *, timeout: int = 60) -> str:
    result = run_command(["git", *args], cwd=repo, timeout=timeout, check=True)
    return result.stdout.strip()


def verify_pinned_checkout(path: pathlib.Path, expected_head: str | None) -> dict[str, Any]:
    if not path.exists():
        raise SmokeError(f"pinned Oxipng checkout is missing: {path}")
    if not (path / ".git").exists():
        raise SmokeError(f"pinned Oxipng checkout is not a git checkout: {path}")
    head = git_output(path, ["rev-parse", "HEAD"])
    status = git_output(path, ["status", "--short"])
    if status:
        raise SmokeError(f"pinned Oxipng checkout must be clean before smoke; status:\n{status}")
    if expected_head and head != expected_head:
        raise SmokeError(f"pinned Oxipng HEAD mismatch: got {head}, expected {expected_head}")
    describe = run_command(["git", "describe", "--tags", "--always", "--dirty"], cwd=path, timeout=60)
    return {
        "head": head,
        "status": status,
        "describe": describe.stdout.strip() if describe.returncode == 0 else None,
    }


def copy_detached_checkout(src: pathlib.Path, dst: pathlib.Path) -> None:
    ignore = shutil.ignore_patterns("target")
    shutil.copytree(src, dst, ignore=ignore)
    status = git_output(dst, ["status", "--short"])
    if status:
        raise SmokeError(f"fresh detached Oxipng copy is unexpectedly dirty:\n{status}")


def insert_after_once(text: str, needle: str, insertion: str) -> str:
    if insertion.strip() in text:
        return text
    index = text.find(needle)
    if index < 0:
        raise SmokeError(f"instrumentation anchor not found: {needle!r}")
    return text[: index + len(needle)] + insertion + text[index + len(needle) :]


def apply_instrumentation(oxipng: pathlib.Path, unialloc_crate: pathlib.Path) -> None:
    relative_unialloc = os.path.relpath(unialloc_crate.resolve(), oxipng.resolve())
    cargo = oxipng / "Cargo.toml"
    cargo_text = cargo.read_text(encoding="utf-8")
    dep_line = f'unialloc = {{ path = "{relative_unialloc}", features = ["stats", "type_isolation"] }}\nlock_api = "=0.4.3"\n'
    cargo_text = insert_after_once(cargo_text, "[dependencies]\n", dep_line)
    if "\n[workspace]\n" not in cargo_text:
        cargo_text = cargo_text.rstrip() + "\n\n[workspace]\n"
    cargo.write_text(cargo_text, encoding="utf-8")

    lib_rs = oxipng / "src" / "lib.rs"
    lib_text = lib_rs.read_text(encoding="utf-8")
    lib_text = insert_after_once(
        lib_text,
        "#[cfg(feature = \"parallel\")]\nextern crate rayon;",
        "\n#[allow(unused_extern_crates)]\nextern crate unialloc;\n",
    )
    lib_rs.write_text(lib_text, encoding="utf-8")

    main_rs = oxipng / "src" / "main.rs"
    main_text = main_rs.read_text(encoding="utf-8")
    import_line = (
        "use unialloc::{semantic_stats_recording_disable, semantic_stats_recording_enable, "
        "semantic_stats_reset, semantic_stats_snapshot, semantic_type_stats_recording_disable, "
        "semantic_type_stats_recording_enable, semantic_type_stats_snapshot, "
        "type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, UniAlloc};\n"
    )
    main_text = insert_after_once(
        main_text,
        "use std::time::Duration;\n",
        import_line + "\n#[global_allocator]\nstatic UNIALLOC: UniAlloc = UniAlloc;\n",
    )
    main_text = insert_after_once(
        main_text,
        "fn main() {\n",
        "    semantic_stats_reset();\n    semantic_stats_recording_enable();\n    semantic_type_stats_recording_enable();\n",
    )
    stats_block = r'''

    let stats = semantic_stats_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 64];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    eprintln!(
        "UNIALLOC_STATS_JSON={{\"source\":\"instrumented-oxipng\",\"total_allocations\":{},\"typed_allocations\":{},\"fallback_allocations\":{},\"typed_deallocations\":{},\"fallback_deallocations\":{},\"typed_cache_hits\":{},\"typed_cache_inserts\":{},\"typed_cache_bypasses\":{},\"coverage_basis_points\":{},\"type_stats_rows\":{},\"type_stats_dropped_events\":{},\"type_isolation_inline_occupied\":{},\"type_isolation_occupied_slots\":{},\"type_isolation_occupied_entries\":{},\"type_isolation_corrupt_slots\":{}}}",
        stats.total_allocations,
        stats.typed_allocations,
        stats.fallback_allocations,
        stats.typed_deallocations,
        stats.fallback_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.typed_cache_bypasses,
        stats.coverage_basis_points,
        row_count,
        stats.semantic_type_stats_dropped_events,
        side_cache.inline_occupied,
        side_cache.occupied_slots,
        side_cache.occupied_entries,
        side_cache.corrupt_slots,
    );
    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
'''
    main_text = insert_after_once(main_text, "    if !success {\n        exit(1);\n    }\n", stats_block)
    main_rs.write_text(main_text, encoding="utf-8")


def write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_pass_binary(toolchain: str, out: pathlib.Path, timeout: int) -> pathlib.Path:
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    result = run_command(
        ["rustc", f"+{toolchain}", str(PASS_SOURCE), "-o", str(out)],
        cwd=ROOT,
        env=env,
        timeout=timeout,
        check=True,
    )
    if not out.exists():
        raise SmokeError("pass binary build reported success but output is missing")
    return out


def require_posix_host(os_name: str = os.name) -> None:
    if os_name != "posix":
        raise SmokeError("oxipng realapp repro smoke is POSIX-only; Windows path/executable handling is not claimed")


def path_overlaps(left: pathlib.Path, right: pathlib.Path) -> bool:
    left_resolved = left.resolve()
    right_resolved = right.resolve()
    return (
        left_resolved == right_resolved
        or left_resolved in right_resolved.parents
        or right_resolved in left_resolved.parents
    )


def reject_protected_path_overlap(path: pathlib.Path, *, pinned: pathlib.Path, label: str) -> None:
    for protected_label, protected in (("repo root", ROOT), ("pinned checkout", pinned)):
        if path_overlaps(path, protected):
            raise SmokeError(
                f"{label} must not overlap or contain {protected_label}: {path.resolve()} vs {protected.resolve()}"
            )


def wrapper_env(
    *,
    base_env: dict[str, str],
    sysroot: pathlib.Path,
    wrapper: pathlib.Path,
    audit_dir: pathlib.Path,
    log_dir: pathlib.Path,
    target_crate: str,
) -> dict[str, str]:
    env = base_env.copy()
    # The pinned Oxipng transitive cloudflare-zlib-sys vendor code predates
    # modern macOS SDK TARGET_OS_MAC headers; undefining the SDK macro keeps the
    # historical offline build path reproducible without patching the checkout.
    if sys.platform == "darwin":
        existing_cflags = env.get("CFLAGS", "")
        compat_flag = "-UTARGET_OS_MAC"
        env["CFLAGS"] = existing_cflags if compat_flag in existing_cflags.split() else (compat_flag + (" " + existing_cflags if existing_cflags else ""))
    lib = str(sysroot / "lib")
    env["DYLD_LIBRARY_PATH"] = lib + (os.pathsep + env["DYLD_LIBRARY_PATH"] if env.get("DYLD_LIBRARY_PATH") else "")
    env["LD_LIBRARY_PATH"] = lib + (os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env.update(
        {
            "RUSTC_WRAPPER": str(wrapper),
            "UNIALLOC_RUSTC_SYSROOT": str(sysroot),
            "UNIALLOC_RUSTC_TARGET_CRATES": target_crate,
            "UNIALLOC_REWRITE_AUDIT_DIR": str(audit_dir),
            "UNIALLOC_PASS_LOG_DIR": str(log_dir),
            "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
            "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
            "UNIALLOC_DIRECT_LOCAL_METADATA_ABI": "1",
            "UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP": "1",
            "UNIALLOC_CONTINUE_COMPILATION": "1",
            "UNIALLOC_LOWERING_POLICY_FLAGS": "1",
        }
    )
    return env


def parse_stats(stderr: str) -> dict[str, Any]:
    matches = [line[len(STATS_PREFIX) :] for line in stderr.splitlines() if line.startswith(STATS_PREFIX)]
    if len(matches) != 1:
        raise SmokeError(f"expected exactly one {STATS_PREFIX} line, found {len(matches)}")
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise SmokeError(f"invalid UniAlloc stats JSON: {exc}") from exc
    return value


def crate_name_from_rustc_args(args: list[Any]) -> str | None:
    for index, value in enumerate(args):
        text = str(value)
        if text == "--crate-name" and index + 1 < len(args):
            return str(args[index + 1])
        if text.startswith("--crate-name="):
            return text.split("=", 1)[1]
    return None


def collect_audits(audit_dir: pathlib.Path, target_crate: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    audits: list[dict[str, Any]] = []
    totals = {
        "direct_rewrite_applied_count": 0,
        "semantic_scope_rewrite_applied_count": 0,
        "semantic_scope_drop_rewrite_applied_count": 0,
        "semantic_scope_unsolved_candidate_count": 0,
        "semantic_scope_drop_unsolved_candidate_count": 0,
    }
    for path in sorted(audit_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        compiler = data.get("compiler_pass", {})
        summary = data.get("summary", {})
        crate_name = compiler.get("crate_name") or data.get("crate_name") or crate_name_from_rustc_args(data.get("rustc_args", []))
        if crate_name != target_crate:
            continue
        row = {
            "file": path.name,
            "crate_name": crate_name,
            "actual_allocator_call_replacement": bool(compiler.get("actual_allocator_call_replacement")),
            "actual_semantic_scope_rewrite": bool(compiler.get("actual_semantic_scope_rewrite")),
            "body_clone_returned_to_rustc": bool(compiler.get("body_clone_returned_to_rustc")),
            "direct_local_metadata_abi": bool(compiler.get("direct_local_metadata_abi")),
            "direct_local_size_align_with_semantic_drop": bool(
                compiler.get("direct_local_size_align_with_semantic_drop")
            ),
            "replacement_resolution_status": compiler.get("replacement_resolution_status")
            or compiler.get("direct_replacement_resolution_status")
            or summary.get("replacement_resolution_status")
            or summary.get("direct_replacement_resolution_status"),
            "rewrite_candidate_count": int(compiler.get("rewrite_candidate_count", summary.get("rewrite_candidate_count", 0)) or 0),
            "direct_rewrite_applied_count": int(compiler.get("rewrite_applied_count", summary.get("rewrite_applied_count", 0)) or 0),
            "semantic_scope_rewrite_applied_count": int(
                compiler.get("semantic_scope_rewrite_applied_count", summary.get("semantic_scope_rewrite_applied_count", 0)) or 0
            ),
            "semantic_scope_drop_rewrite_applied_count": int(
                compiler.get("semantic_scope_drop_rewrite_applied_count", summary.get("semantic_scope_drop_rewrite_applied_count", 0)) or 0
            ),
            "semantic_scope_unsolved_candidate_count": int(
                compiler.get("semantic_scope_unsolved_candidate_count", summary.get("semantic_scope_unsolved_candidate_count", 0)) or 0
            ),
            "semantic_scope_drop_unsolved_candidate_count": int(
                compiler.get("semantic_scope_drop_unsolved_candidate_count", summary.get("semantic_scope_drop_unsolved_candidate_count", 0)) or 0
            ),
        }
        audits.append(row)
        for key in totals:
            totals[key] += int(row[key])
    return audits, totals



def compiler_coverage_summary(audit_totals: dict[str, int]) -> dict[str, Any]:
    unsolved_semantic = int(audit_totals.get("semantic_scope_unsolved_candidate_count", 0))
    unsolved_drop = int(audit_totals.get("semantic_scope_drop_unsolved_candidate_count", 0))
    unsolved_total = unsolved_semantic + unsolved_drop
    return {
        "audited_candidates_resolved": unsolved_total == 0,
        "has_unresolved_audited_candidates": unsolved_total != 0,
        "coverage_scope": "target_crate_audited_semantic_and_drop_candidates",
        "whole_program_compiler_coverage": False,
        "unsolved_candidate_count": unsolved_total,
        "semantic_scope_unsolved_candidate_count": unsolved_semantic,
        "semantic_scope_drop_unsolved_candidate_count": unsolved_drop,
        "direct_rewrite_applied_count": int(audit_totals.get("direct_rewrite_applied_count", 0)),
        "semantic_scope_rewrite_applied_count": int(audit_totals.get("semantic_scope_rewrite_applied_count", 0)),
        "semantic_scope_drop_rewrite_applied_count": int(audit_totals.get("semantic_scope_drop_rewrite_applied_count", 0)),
        "claim_boundary": (
            "no unresolved supported semantic/drop candidates in audited target-crate MIR; "
            "not whole-program or object coverage"
            if unsolved_total == 0
            else "audited target-crate MIR still has explicitly counted unresolved "
            "semantic/drop candidates; not whole-program or object coverage"
        ),
    }

def assert_contract(
    *,
    build: CommandResult,
    run: CommandResult,
    stats: dict[str, Any],
    output_sha256: str,
    expected_output_sha256: str,
    audits: list[dict[str, Any]],
    audit_totals: dict[str, int],
    fallback_note: str,
) -> None:
    if build.returncode != 0:
        raise SmokeError(f"wrapper cargo build failed with {build.returncode}")
    if run.returncode != 0:
        raise SmokeError(f"instrumented Oxipng run failed with {run.returncode}")
    if output_sha256 != expected_output_sha256:
        raise SmokeError(f"output hash mismatch: got {output_sha256}, expected {expected_output_sha256}")
    if not audits:
        raise SmokeError("no target-crate rewrite audits were emitted")
    false_flags = []
    for audit in audits:
        required_flags = SEMANTIC_AND_BODY_AUDIT_FLAGS
        if audit.get("replacement_resolution_status") != NO_SUPPORTED_DIRECT_REWRITE_STATUS:
            required_flags += DIRECT_REWRITE_AUDIT_FLAGS
        false_flags.extend(
            f"{audit.get('file')}:{flag}"
            for flag in required_flags
            if not audit.get(flag)
        )
    if false_flags:
        raise SmokeError("target-crate audit flags must be true: " + ", ".join(false_flags))
    if audit_totals["direct_rewrite_applied_count"] <= 0:
        raise SmokeError("direct MIR allocator rewrites must be nonzero")
    if audit_totals["semantic_scope_rewrite_applied_count"] <= 0:
        raise SmokeError("semantic scope rewrites must be nonzero")
    if audit_totals["semantic_scope_drop_rewrite_applied_count"] <= 0:
        raise SmokeError("semantic drop rewrites must be nonzero")
    if int(stats.get("typed_allocations", 0)) <= 0:
        raise SmokeError("typed allocations must be nonzero")
    if "fallback_allocations" not in stats:
        raise SmokeError("fallback allocations must be explicitly reported")
    if int(stats.get("type_isolation_corrupt_slots", -1)) != 0:
        raise SmokeError("type isolation corrupt slots must be zero")
    if not fallback_note:
        raise SmokeError("fallback note must be explicit")


def deterministic_summary(summary: dict[str, Any]) -> str:
    return json.dumps(summary, indent=2, sort_keys=True) + "\n"


def load_reference_summary(path: pathlib.Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if path.is_dir():
        path = path / "instrumented-oxipng-summary.json"
    if not path.exists():
        raise SmokeError(f"reference evidence summary is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def require_fresh_dir(path: pathlib.Path, *, label: str) -> None:
    if path.exists():
        if not path.is_dir():
            raise SmokeError(f"{label} path exists and is not a directory: {path}")
        try:
            next(path.iterdir())
        except StopIteration:
            return
        raise SmokeError(f"{label} directory must be fresh/empty; refusing to reuse existing files: {path}")




def require_fresh_output_dir(path: pathlib.Path) -> None:
    require_fresh_dir(path, label="output")


def require_fresh_temp_dir(path: pathlib.Path) -> None:
    require_fresh_dir(path, label="temporary")


def path_is_relative_to(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def require_contained_path(path: pathlib.Path, parent: pathlib.Path, *, label: str) -> None:
    if not path_is_relative_to(path, parent):
        raise SmokeError(f"{label} must be contained inside detached checkout: {path.resolve()} not under {parent.resolve()}")


def git_status_for_paths(repo: pathlib.Path, paths: list[pathlib.Path]) -> str:
    return git_output(repo, ["status", "--short", "--", *[str(path) for path in paths]])


def source_file_hashes(paths: list[pathlib.Path]) -> dict[str, str]:
    files: list[pathlib.Path] = []
    for path in paths:
        absolute = ROOT / path
        if absolute.is_dir():
            files.extend(sorted(child for child in absolute.rglob("*") if child.is_file()))
        elif absolute.is_file():
            files.append(absolute)
        else:
            raise SmokeError(f"source binding path is missing: {path}")
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in sorted(files)}


def scoped_fingerprint(file_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, file_hash in sorted(file_hashes.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def source_binding(toolchain: str, sysroot: pathlib.Path, rustc_verbose_version: str) -> dict[str, Any]:
    paths = SCOPED_STATUS_PATHS
    file_hashes = source_file_hashes(paths)
    return {
        "repo_head": git_output(ROOT, ["rev-parse", "HEAD"]),
        "scoped_status": git_status_for_paths(ROOT, paths),
        "scoped_status_paths": [str(path) for path in paths],
        "scoped_file_count": len(file_hashes),
        "scoped_fingerprint_sha256": scoped_fingerprint(file_hashes),
        "scoped_file_hashes": file_hashes,
        "pass_source": str(PASS_SOURCE.relative_to(ROOT)),
        "pass_source_sha256": sha256_file(PASS_SOURCE),
        "repo_cargo_lock_sha256": sha256_file(ROOT / "Cargo.lock"),
        "build_toolchain": toolchain,
        "rustc_sysroot": str(sysroot),
        "rustc_verbose_version": rustc_verbose_version,
    }


def reject_source_drift(start: dict[str, Any], end: dict[str, Any]) -> None:
    keys = (
        "repo_head",
        "scoped_status",
        "scoped_fingerprint_sha256",
        "pass_source_sha256",
        "repo_cargo_lock_sha256",
        "rustc_sysroot",
        "rustc_verbose_version",
    )
    drift = {key: {"start": start.get(key), "end": end.get(key)} for key in keys if start.get(key) != end.get(key)}
    if drift:
        raise SmokeError(f"source binding drifted during smoke: {json.dumps(drift, sort_keys=True)}")


def pass_binary_binding(
    path: pathlib.Path,
    *,
    built_by_script: bool,
    expected_sha256: str | None = None,
    provenance: str | None = None,
) -> dict[str, Any]:
    if not path.exists():
        raise SmokeError(f"pass binary is missing: {path}")
    digest = sha256_file(path)
    if not built_by_script:
        if not expected_sha256:
            raise SmokeError("provided pass binary requires --expected-pass-binary-sha256; refusing unproven current-source-bound success")
        if not provenance:
            raise SmokeError("provided pass binary requires --pass-binary-provenance; refusing unproven current-source-bound success")
        if digest != expected_sha256:
            raise SmokeError(f"provided pass binary hash mismatch: got {digest}, expected {expected_sha256}")
    return {
        "path": str(path),
        "sha256": digest,
        "expected_sha256": expected_sha256,
        "provenance": provenance,
        "built_by_script": built_by_script,
        "source_boundary": (
            "script built this binary from the recorded pass source in the current run"
            if built_by_script
            else "provided pass binary is bound by binary hash; current pass source hash/status is recorded but is not binary provenance"
        ),
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    require_posix_host()
    pinned = args.pinned_checkout.resolve()
    output_dir = args.output_dir.resolve()
    reject_protected_path_overlap(output_dir, pinned=pinned, label="output directory")
    require_fresh_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.temp_parent is None and pathlib.Path("/private/tmp").is_dir():
        args.temp_parent = "/private/tmp"
    if args.temp_parent is not None:
        reject_protected_path_overlap(pathlib.Path(args.temp_parent), pinned=pinned, label="temporary parent")
    temp_owner: tempfile.TemporaryDirectory[str] | None = None
    try:
        temp_root = args.temp_dir
        if temp_root is None:
            temp_owner = tempfile.TemporaryDirectory(prefix="unialloc-oxipng-smoke-", dir=args.temp_parent)
            temp_root = pathlib.Path(temp_owner.name)
        else:
            temp_root = temp_root.resolve()
            reject_protected_path_overlap(temp_root, pinned=pinned, label="temporary directory")
            require_fresh_temp_dir(temp_root)
            temp_root.mkdir(parents=True, exist_ok=True)
        reject_protected_path_overlap(temp_root, pinned=pinned, label="temporary directory")

        pinned_before = verify_pinned_checkout(pinned, args.expected_head)
        oxipng = temp_root / "oxipng-instrumented"
        copy_detached_checkout(pinned, oxipng)
        apply_instrumentation(oxipng, ROOT / "unialloc")

        patch_text = git_output(oxipng, ["diff", "--binary", "--", "Cargo.toml", "src/lib.rs", "src/main.rs"])
        if not patch_text.strip():
            raise SmokeError("instrumentation patch is empty")
        patch_path = output_dir / "instrumentation.patch"
        write_text(patch_path, patch_text + "\n")

        sysroot_text = run_command(
            ["rustc", f"+{args.toolchain}", "--print", "sysroot"], cwd=ROOT, timeout=args.timeout, check=True
        ).stdout.strip()
        sysroot = pathlib.Path(sysroot_text)
        rustc_verbose_version = run_command(
            ["rustc", f"+{args.toolchain}", "-vV"], cwd=ROOT, timeout=args.timeout, check=True
        ).stdout.strip()
        source_binding_start = source_binding(args.toolchain, sysroot, rustc_verbose_version)
        wrapper = args.pass_binary.resolve() if args.pass_binary else temp_root / "unialloc-rustc-mir-rewrite-dry-run"
        pass_built_by_script = args.pass_binary is None
        if args.pass_binary is None:
            build_pass_binary(args.toolchain, wrapper, args.timeout)
        elif not wrapper.exists():
            raise SmokeError(f"provided pass binary does not exist: {wrapper}")
        pass_binding = pass_binary_binding(
            wrapper,
            built_by_script=pass_built_by_script,
            expected_sha256=args.expected_pass_binary_sha256,
            provenance=args.pass_binary_provenance,
        )

        audit_dir = output_dir / "rewrites"
        log_dir = output_dir / "logs"
        audit_dir.mkdir(exist_ok=True)
        log_dir.mkdir(exist_ok=True)
        target_dir = temp_root / "target-wrapper"
        build_env = wrapper_env(
            base_env=os.environ.copy(),
            sysroot=sysroot,
            wrapper=wrapper,
            audit_dir=audit_dir,
            log_dir=log_dir,
            target_crate=args.target_crate,
        )
        build = run_command(
            [
                "cargo",
                f"+{args.toolchain}",
                "build",
                "--offline",
                "--features",
                "binary,parallel",
                "--package",
                args.target_crate,
                "--target-dir",
                str(target_dir),
            ],
            cwd=oxipng,
            env=build_env,
            timeout=args.timeout,
        )
        write_text(output_dir / "wrapper-build.stdout.txt", build.stdout)
        write_text(output_dir / "wrapper-build.stderr.txt", build.stderr)
        write_text(output_dir / "wrapper-build.returncode.txt", f"{build.returncode}\n")
        if build.returncode != 0:
            raise SmokeError(f"wrapper cargo build failed with {build.returncode}; see {output_dir / 'wrapper-build.stderr.txt'}")

        input_path = (oxipng / args.input).resolve()
        require_contained_path(input_path, oxipng, label="input PNG")
        if not input_path.exists():
            raise SmokeError(f"input PNG is missing in detached copy: {input_path}")
        wrapper_output = output_dir / "wrapper-out.png"
        binary = target_dir / "debug" / "oxipng"
        run = run_command(
            [str(binary), str(input_path), "--out", str(wrapper_output), "--force", "--threads", "1"],
            cwd=oxipng,
            env=build_env,
            timeout=args.timeout,
        )
        write_text(output_dir / "functional.stdout.txt", run.stdout)
        write_text(output_dir / "functional.stderr.txt", run.stderr)
        write_text(output_dir / "functional.returncode.txt", f"{run.returncode}\n")
        if not wrapper_output.exists():
            raise SmokeError("functional run did not produce wrapper-out.png")
        built_oxipng_binary_sha256 = sha256_file(binary)
        stats = parse_stats(run.stderr)
        output_sha = sha256_file(wrapper_output)
        input_sha = sha256_file(input_path)
        audits, audit_totals = collect_audits(audit_dir, args.target_crate)
        fallback_note = (
            f"reported fallback_allocations={stats.get('fallback_allocations')} and "
            f"fallback_deallocations={stats.get('fallback_deallocations')}"
        )
        assert_contract(
            build=build,
            run=run,
            stats=stats,
            output_sha256=output_sha,
            expected_output_sha256=args.expected_output_sha256,
            audits=audits,
            audit_totals=audit_totals,
            fallback_note=fallback_note,
        )
        pinned_after = verify_pinned_checkout(pinned, pinned_before["head"])
        source_binding_end = source_binding(args.toolchain, sysroot, rustc_verbose_version)
        reject_source_drift(source_binding_start, source_binding_end)
        reference = load_reference_summary(args.reference_evidence)
        reference_digest = None
        if args.reference_evidence is not None:
            evidence_path = args.reference_evidence
            if evidence_path.is_dir():
                evidence_path = evidence_path / "instrumented-oxipng-summary.json"
            reference_digest = sha256_file(evidence_path)

        summary = {
            "schema_version": 1,
            "source": "oxipng-realapp-repro-smoke",
            "boundary": (
                "functional/diagnostic smoke only; no timing loop; no performance claim; "
                "subprocess timeout descendant cleanup is not claimed beyond direct fail-closed timeout handling"
            ),
            "toolchain": args.toolchain,
            "build_toolchain": source_binding_start["build_toolchain"],
            "rustc_sysroot": source_binding_start["rustc_sysroot"],
            "rustc_verbose_version": source_binding_start["rustc_verbose_version"],
            "rustc_target_crates": args.target_crate,
            "pinned_checkout": {
                "head": pinned_before["head"],
                "describe": pinned_before["describe"],
                "status_before": pinned_before["status"],
                "status_after": pinned_after["status"],
            },
            "reference_evidence_sha256": reference_digest,
            "reference_evidence_source": None if reference is None else reference.get("source"),
            "source_binding": {
                "start": source_binding_start,
                "end": source_binding_end,
                "drift_checked": True,
            },
            "instrumented_copy_status": git_output(oxipng, ["status", "--short"]),
            "instrumentation_patch": patch_path.name,
            "instrumentation_patch_sha256": sha256_file(patch_path),
            "instrumented_cargo_lock_sha256": sha256_file(oxipng / "Cargo.lock"),
            "pass_binary": pass_binding,
            "pass_binary_sha256": pass_binding["sha256"],
            "build": {
                "command": "cargo +{toolchain} build --offline --features binary,parallel --package {crate}".format(
                    toolchain=args.toolchain, crate=args.target_crate
                ),
                "returncode": build.returncode,
                "stdout": "wrapper-build.stdout.txt",
                "stderr": "wrapper-build.stderr.txt",
                "built_binary": str(binary),
                "built_binary_sha256": built_oxipng_binary_sha256,
            },
            "functional_run": {
                "command": "target-wrapper/debug/oxipng <input> --out wrapper-out.png --force --threads 1",
                "returncode": run.returncode,
                "input": str(args.input),
                "input_bytes": input_path.stat().st_size,
                "input_sha256": input_sha,
                "output": wrapper_output.name,
                "output_bytes": wrapper_output.stat().st_size,
                "output_sha256": output_sha,
                "expected_output_sha256": args.expected_output_sha256,
                "equivalent_to_known_hash": output_sha == args.expected_output_sha256,
                "stderr": "functional.stderr.txt",
                "stdout": "functional.stdout.txt",
                "unialloc_stats": stats,
                "fallback_report": fallback_note,
            },
            "audit_file_count": len(audits),
            "audit_totals": audit_totals,
            "compiler_coverage": compiler_coverage_summary(audit_totals),
            "audits": audits,
        }
        write_text(output_dir / "oxipng-realapp-repro-summary.json", deterministic_summary(summary))
        return summary
    finally:
        if temp_owner is not None:
            if args.keep_temp:
                # Detach the finalizer so callers can inspect the exact detached copy.
                print(f"kept temp directory: {temp_owner.name}", file=sys.stderr)
                temp_owner._finalizer.detach()  # type: ignore[attr-defined]
            else:
                temp_owner.cleanup()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pinned-checkout", type=pathlib.Path, default=DEFAULT_PINNED_CHECKOUT)
    parser.add_argument("--expected-head", default="dea23211ae6259007e068c59ab16929798d00d96")
    parser.add_argument("--reference-evidence", type=pathlib.Path)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--temp-dir", type=pathlib.Path)
    parser.add_argument("--temp-parent", type=str, default=None)
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--toolchain", default=DEFAULT_TOOLCHAIN)
    parser.add_argument("--target-crate", default=DEFAULT_TARGET_CRATE)
    parser.add_argument("--input", type=pathlib.Path, default=DEFAULT_INPUT)
    parser.add_argument("--expected-output-sha256", default=DEFAULT_EXPECTED_OUTPUT_SHA256)
    parser.add_argument("--pass-binary", type=pathlib.Path)
    parser.add_argument("--expected-pass-binary-sha256")
    parser.add_argument("--pass-binary-provenance")
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        summary = run_smoke(args)
    except (SmokeError, subprocess.TimeoutExpired) as exc:
        print(f"oxipng realapp repro smoke failed: {exc}", file=sys.stderr)
        return 1
    print(deterministic_summary(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
