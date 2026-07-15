#!/usr/bin/env python3
"""Run defensible UniAlloc and cross-allocator large-page comparisons.

The experiment deliberately has two families.  The semantic family compares
UniAlloc lifetime-directed THP against the identical lifetime policy with
process THP backing disabled.  The neutral family sends one malloc/free trace
to general-purpose allocators and measures each allocator's own large-page
configuration delta.  Raw timings never cross those family boundaries.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifetime_thp_allocator_experiment as lifetime_thp
import google_tcmalloc_bazel_probe as google_tcmalloc


ROOT = Path(__file__).resolve().parents[2]
NEUTRAL_SOURCE = ROOT / "evaluation/probes/cross_allocator_large_page_workload.c"
SEMANTIC_BINARY = ROOT / "target/release/examples/lifetime_hugepage_allocator_probe"
DEFAULT_GPERFTOOLS_LEGACY = Path(
    "/home/hanqing/.local/state/unialloc/runs/"
    "g001-minimal-235549b2-20260711T155042Z/evidence/"
    "pilot-controller/repairs/tcmalloc-full-runtime-v1-20260711T1717Z-attempt2/"
    "lib/libtcmalloc.so.4.6.5"
)
DEFAULT_JEMALLOC = Path("/usr/lib/x86_64-linux-gnu/libjemalloc.so.2")
LOCAL_BUILD_ROOT = ROOT / "evaluation/raw/cross-allocator-local-builds"
DEFAULT_MIMALLOC = LOCAL_BUILD_ROOT / "mimalloc-manual/libmimalloc.so"
DEFAULT_SNMALLOC = LOCAL_BUILD_ROOT / "snmalloc/libsnmallocshim.so"
SANITIZED_ENV_PREFIXES = ("MIMALLOC_", "TCMALLOC_")
SANITIZED_ENV_KEYS = {
    "LD_PRELOAD",
    "MALLOC_CONF",
    "_RJEM_MALLOC_CONF",
    "UNIALLOC_EVAL_PRELOAD_TOKEN",
    "UNIALLOC_EVAL_THP_DISABLE",
}


@dataclass(frozen=True)
class SemanticCase:
    name: str
    label: str
    policy: str
    expected_backing: str
    mechanism: str
    disable_process_thp: bool = False


@dataclass(frozen=True)
class NeutralCase:
    name: str
    label: str
    mechanism: str
    expected_backing: str
    library_key: str | None
    environment: tuple[tuple[str, str], ...] = ()
    disable_process_thp: bool = False


SEMANTIC_CASES = (
    SemanticCase(
        "unialloc_default",
        "UniAlloc default",
        "raw-default",
        "system-default",
        "allocator-default",
    ),
    SemanticCase(
        "unialloc_ordinary",
        "UniAlloc ordinary arenas",
        "ordinary-segregated",
        "ordinary-no-thp",
        "lifetime-segregated-ordinary",
    ),
    SemanticCase(
        "unialloc_lifetime_thp_off",
        "UniAlloc lifetime layout / THP off",
        "long-thp",
        "ordinary-no-thp",
        "lifetime-layout-thp-disabled",
        True,
    ),
    SemanticCase(
        "unialloc_lifetime_thp_on",
        "UniAlloc selective lifetime THP",
        "long-thp",
        "thp",
        "selective-lifetime-thp",
    ),
)

CANONICAL_MECHANISM_BY_CASE = {
    case.name: case.mechanism for case in SEMANTIC_CASES
} | {
    # Historical evidence used the broader process-wide label. opt.thp controls
    # jemalloc-managed default extents while metadata THP remains disabled.
    "jemalloc_thp_off": "allocator-wide-thp",
    "jemalloc_thp_on": "allocator-wide-thp",
}


def neutral_cases(include_gperftools_legacy: bool = False) -> tuple[NeutralCase, ...]:
    mimalloc_common = (
        ("MIMALLOC_ALLOW_LARGE_OS_PAGES", "0"),
        ("MIMALLOC_RESERVE_HUGE_OS_PAGES", "0"),
        # Hold purge granularity constant so the THP pair isolates eligibility.
        ("MIMALLOC_MINIMAL_PURGE_SIZE", "2048"),
    )
    cases = (
        NeutralCase(
            "glibc_default", "glibc default", "default", "default", None
        ),
        NeutralCase(
            "google_tcmalloc_temeraire",
            "Google TCMalloc / Temeraire HPAA",
            "temeraire-hpaa",
            "default",
            None,
        ),
        NeutralCase(
            "mimalloc_thp_off",
            "mimalloc THP off",
            "process-wide-thp",
            "no-thp",
            "mimalloc",
            mimalloc_common + (("MIMALLOC_ALLOW_THP", "0"),),
        ),
        NeutralCase(
            "mimalloc_thp_on",
            "mimalloc THP on",
            "process-wide-thp",
            "thp",
            "mimalloc",
            mimalloc_common + (("MIMALLOC_ALLOW_THP", "1"),),
        ),
        NeutralCase(
            "jemalloc_thp_off",
            "jemalloc THP off",
            "allocator-wide-thp",
            "no-thp",
            "jemalloc",
            (("MALLOC_CONF", "abort_conf:true,thp:never,metadata_thp:disabled"),),
        ),
        NeutralCase(
            "jemalloc_thp_on",
            "jemalloc THP on",
            "allocator-wide-thp",
            "thp",
            "jemalloc",
            (("MALLOC_CONF", "abort_conf:true,thp:always,metadata_thp:disabled"),),
        ),
        NeutralCase(
            "snmalloc_default",
            "snmalloc default eligibility",
            "os-eligibility",
            "default",
            "snmalloc",
        ),
        NeutralCase(
            "snmalloc_os_thp_off",
            "snmalloc process THP disabled",
            "os-eligibility",
            "no-thp",
            "snmalloc",
            disable_process_thp=True,
        ),
    )
    if not include_gperftools_legacy:
        return cases
    return cases + (
        NeutralCase(
            "gperftools_legacy_default",
            "gperftools 2.18.1 legacy default",
            "legacy-default",
            "default",
            "gperftools_legacy",
            (("TCMALLOC_MEMFS_MALLOC_PATH", ""),),
        ),
        NeutralCase(
            "gperftools_legacy_hugetlb",
            "gperftools 2.18.1 legacy explicit HugeTLB",
            "legacy-explicit-hugetlb",
            "hugetlb",
            "gperftools_legacy",
            (("TCMALLOC_MEMFS_DISABLE_FALLBACK", "1"),),
        ),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=12)
    parser.add_argument("--warmup-blocks", type=int, default=1)
    parser.add_argument("--bootstrap-resamples", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--cpu", type=int, default=15)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--objects", type=int, default=131_072)
    parser.add_argument("--slot-bytes", type=int, default=4096)
    parser.add_argument("--passes", type=int, default=32)
    parser.add_argument("--warmup-passes", type=int, default=2)
    parser.add_argument("--waves", type=int, default=2)
    parser.add_argument("--settle-ms", type=int, default=25)
    parser.add_argument("--types-per-truth", type=int, default=8)
    parser.add_argument("--false-long-rate", type=float, default=0.05)
    parser.add_argument("--false-short-rate", type=float, default=0.05)
    parser.add_argument("--min-semantic-thp-coverage", type=float, default=0.95)
    parser.add_argument("--min-neutral-thp-coverage", type=float, default=0.25)
    parser.add_argument("--mimalloc-lib", type=Path, default=DEFAULT_MIMALLOC)
    parser.add_argument("--jemalloc-lib", type=Path, default=DEFAULT_JEMALLOC)
    parser.add_argument(
        "--google-tcmalloc-build-root",
        type=Path,
        default=google_tcmalloc.DEFAULT_BUILD_ROOT,
    )
    parser.add_argument(
        "--google-tcmalloc-binary", type=Path
    )
    parser.add_argument(
        "--google-tcmalloc-control-binary",
        type=Path,
    )
    parser.add_argument(
        "--google-tcmalloc-provenance",
        type=Path,
    )
    parser.add_argument("--bazel", default=os.environ.get("BAZEL", "bazel"))
    parser.add_argument(
        "--gperftools-legacy-lib", type=Path, default=DEFAULT_GPERFTOOLS_LEGACY
    )
    parser.add_argument("--include-gperftools-legacy", action="store_true")
    parser.add_argument("--snmalloc-lib", type=Path, default=DEFAULT_SNMALLOC)
    parser.add_argument("--gperftools-legacy-memfs-prefix", type=Path)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args(argv)
    build_root = args.google_tcmalloc_build_root.expanduser().resolve()
    args.google_tcmalloc_build_root = build_root
    if args.google_tcmalloc_binary is None:
        args.google_tcmalloc_binary = (
            build_root / "bin/cross_allocator_large_page_workload_google_tcmalloc"
        )
    if args.google_tcmalloc_control_binary is None:
        args.google_tcmalloc_control_binary = (
            build_root / "bin/cross_allocator_large_page_workload_system"
        )
    if args.google_tcmalloc_provenance is None:
        args.google_tcmalloc_provenance = build_root / "provenance.json"
    if (
        args.repeats < 2
        or args.warmup_blocks < 0
        or args.bootstrap_resamples < 100
        or args.objects < 4
        or args.objects % 2
        or args.slot_bytes < 128
        or args.passes < 1
        or args.waves < 1
        or args.settle_ms < 0
        or args.types_per_truth < 1
        or not 0 <= args.false_long_rate <= 0.5
        or not 0 <= args.false_short_rate <= 0.5
        or not 0 < args.min_semantic_thp_coverage <= 1
        or not 0 < args.min_neutral_thp_coverage <= 1
    ):
        parser.error("invalid experiment geometry or evidence threshold")
    return args


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_output(command: list[str]) -> str:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    ).stdout.strip()


def git_text(*args: str) -> str:
    return checked_output(["git", *args])


def hugepages_free() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("HugePages_Free:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def host_snapshot() -> str:
    paths = (
        "/sys/kernel/mm/transparent_hugepage/enabled",
        "/sys/kernel/mm/transparent_hugepage/defrag",
        "/sys/kernel/mm/transparent_hugepage/hpage_pmd_size",
    )
    parts = [
        f"captured_at={time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"uname={checked_output(['uname', '-a'])}",
        "lscpu:\n" + checked_output(["lscpu"]),
        "meminfo:\n"
        + "\n".join(
            line
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
            if "Huge" in line or line.startswith("MemAvailable:")
        ),
    ]
    for raw in paths:
        path = Path(raw)
        parts.append(f"{raw}={path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts) + "\n"


def library_provenance(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    configured = {
        "mimalloc": (args.mimalloc_lib, "mimalloc core 3.3.2"),
        "jemalloc": (args.jemalloc_lib, "jemalloc 5.3.x system package"),
        "snmalloc": (args.snmalloc_lib, "snmalloc 0.2.27 vendored build"),
    }
    if args.include_gperftools_legacy:
        configured["gperftools_legacy"] = (
            args.gperftools_legacy_lib,
            "gperftools-legacy 2.18.1",
        )
    result: dict[str, dict[str, Any]] = {}
    for key, (path, version) in configured.items():
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise RuntimeError(f"missing {key} library: {resolved}")
        result[key] = {
            "path": str(resolved),
            "sha256": sha256_file(resolved),
            "version_label": version,
        }
    return result


def google_tcmalloc_provenance(args: argparse.Namespace) -> dict[str, Any]:
    provenance_path = args.google_tcmalloc_provenance.expanduser().resolve()
    build_root = args.google_tcmalloc_build_root.expanduser().resolve()
    if not args.skip_build:
        command = [
            sys.executable,
            str(Path(google_tcmalloc.__file__).resolve()),
            "--build-root",
            str(build_root),
            "--provenance",
            str(provenance_path),
            "--bazel",
            args.bazel,
        ]
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode != 0:
            reason = "Google TCMalloc/Temeraire build helper returned BLOCKED"
            if provenance_path.is_file():
                blocked = json.loads(provenance_path.read_text(encoding="utf-8"))
                details = blocked.get("blocked_reasons", [])
                if details:
                    reason += ": " + "; ".join(str(detail) for detail in details)
            raise RuntimeError(reason)
    if not provenance_path.is_file():
        raise RuntimeError(
            "Google TCMalloc/Temeraire arm BLOCKED: missing verified provenance "
            f"{provenance_path}"
        )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if not isinstance(provenance, dict):
        raise RuntimeError(
            "Google TCMalloc/Temeraire arm BLOCKED: provenance root must be an object"
        )
    expected = {
        "status": "VERIFIED",
        "claim_eligible": True,
        "fallback_allowed": False,
        "allocator_identity": "Google TCMalloc",
        "repository": google_tcmalloc.GOOGLE_TCMALLOC_REPOSITORY,
        "commit": google_tcmalloc.GOOGLE_TCMALLOC_COMMIT,
        "commit_date": google_tcmalloc.GOOGLE_TCMALLOC_COMMIT_DATE,
        "pin_policy": (
            "tested 2025 compatibility pin; upstream-current tracking is separate"
        ),
        "revision_role": "compatibility-pin-not-latest",
        "required_bazel_version": google_tcmalloc.REQUIRED_BAZEL_VERSION,
        "pinned_module_release": google_tcmalloc.GOOGLE_TCMALLOC_MODULE_VERSION,
        "rules_cc_module_version": google_tcmalloc.RULES_CC_MODULE_VERSION,
        "bazel_target": google_tcmalloc.BAZEL_TARGET,
        "bazel_control_target": google_tcmalloc.BAZEL_CONTROL_TARGET,
        "bazel_targets": list(google_tcmalloc.BAZEL_TARGETS),
        "malloc_target": google_tcmalloc.MALLOC_TARGET,
        "hugepage_allocator_identity": "Temeraire / HugePageAwareAllocator (HPAA)",
        "official_integration": "Bazel cc_binary malloc attribute at link time",
        "build_options": list(google_tcmalloc.BUILD_OPTIONS),
        "probe_copts": list(google_tcmalloc.PROBE_COPTS),
        "implementation_files": list(google_tcmalloc.IMPLEMENTATION_FILES),
        "source": str(google_tcmalloc.SOURCE.relative_to(ROOT)),
        "source_sha256": sha256_file(google_tcmalloc.SOURCE),
    }
    mismatches = [
        f"{key}={provenance.get(key)!r}"
        for key, value in expected.items()
        if provenance.get(key) != value
    ]
    if provenance.get("gperftools_legacy_eligible_as_google_tcmalloc") is not False:
        mismatches.append("gperftools_legacy_eligible_as_google_tcmalloc")
    if provenance.get("blocked_reasons") not in ([], None):
        mismatches.append("blocked_reasons")
    if provenance.get("build_root") != str(build_root):
        mismatches.append("build_root")

    checkout = build_root / "google-tcmalloc"
    workspace = build_root / "probe-workspace"
    if provenance.get("checkout_path") != str(checkout):
        mismatches.append("checkout_path")
    if provenance.get("workspace_path") != str(workspace):
        mismatches.append("workspace_path")
    if not (checkout / ".git").is_dir():
        mismatches.append("checkout_missing")
    else:
        observed_commit = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout.strip()
        if observed_commit != google_tcmalloc.GOOGLE_TCMALLOC_COMMIT:
            mismatches.append("checkout_commit")
        observed_status = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout.strip()
        if observed_status:
            mismatches.append("checkout_dirty")

    implementation_hashes = provenance.get("implementation_file_hashes")
    implementation_by_path = {
        row.get("path"): row.get("sha256")
        for row in implementation_hashes
        if isinstance(row, dict)
    } if isinstance(implementation_hashes, list) else {}
    if set(implementation_by_path) != set(google_tcmalloc.IMPLEMENTATION_FILES):
        mismatches.append("implementation_file_hashes")
    else:
        for relative, recorded_hash in implementation_by_path.items():
            path = checkout / str(relative)
            if not path.is_file() or sha256_file(path) != recorded_hash:
                mismatches.append(f"implementation_hash:{relative}")

    workspace_hashes = provenance.get("workspace_files")
    workspace_by_path = {
        row.get("path"): row.get("sha256")
        for row in workspace_hashes
        if isinstance(row, dict)
    } if isinstance(workspace_hashes, list) else {}
    required_workspace_files = {
        "MODULE.bazel",
        "BUILD.bazel",
        google_tcmalloc.SOURCE.name,
    }
    if set(workspace_by_path) != required_workspace_files:
        mismatches.append("workspace_files")
    else:
        for relative, recorded_hash in workspace_by_path.items():
            path = workspace / str(relative)
            if not path.is_file() or sha256_file(path) != recorded_hash:
                mismatches.append(f"workspace_hash:{relative}")
    workspace_build = workspace / "BUILD.bazel"
    if workspace_build.is_file():
        expected_malloc_assignment = (
            f'malloc = "{google_tcmalloc.MALLOC_TARGET}"'
        )
        if expected_malloc_assignment not in workspace_build.read_text(encoding="utf-8"):
            mismatches.append("workspace_malloc_target")

    build_command = provenance.get("build_command")
    if not (
        isinstance(build_command, list)
        and len(build_command)
        == len(google_tcmalloc.BUILD_OPTIONS) + len(google_tcmalloc.BAZEL_TARGETS) + 2
        and build_command[0] == provenance.get("bazel_executable")
        and build_command[1] == "build"
        and build_command[2 : 2 + len(google_tcmalloc.BUILD_OPTIONS)]
        == list(google_tcmalloc.BUILD_OPTIONS)
        and build_command[-len(google_tcmalloc.BAZEL_TARGETS) :]
        == list(google_tcmalloc.BAZEL_TARGETS)
    ):
        mismatches.append("build_command")
    bazel_version = provenance.get("bazel_version")
    if bazel_version != f"bazel {google_tcmalloc.REQUIRED_BAZEL_VERSION}":
        mismatches.append("bazel_version")
    if provenance.get("bazel_environment") != {
        "USE_BAZEL_VERSION": google_tcmalloc.REQUIRED_BAZEL_VERSION,
    }:
        mismatches.append("bazel_environment")
    if not isinstance(provenance.get("commit_time"), str) or not isinstance(
        provenance.get("commit_subject"), str
    ):
        mismatches.append("commit_metadata")
    action_graph = provenance.get("bazel_action_graph")
    if not isinstance(action_graph, dict):
        mismatches.append("bazel_action_graph")
    else:
        action_graph_path = action_graph.get("path")
        if not isinstance(action_graph_path, str):
            mismatches.append("bazel_action_graph:path")
        else:
            resolved_action_graph = Path(action_graph_path).expanduser().resolve()
            if (
                not resolved_action_graph.is_file()
                or action_graph.get("sha256") != sha256_file(resolved_action_graph)
                or action_graph.get("bytes") != resolved_action_graph.stat().st_size
            ):
                mismatches.append("bazel_action_graph:evidence")
        aquery_command = action_graph.get("command")
        if not (
            isinstance(aquery_command, list)
            and len(aquery_command) == 4
            and aquery_command[0] == provenance.get("bazel_executable")
            and aquery_command[1:3] == ["aquery", "--output=textproto"]
            and all(target in aquery_command[3] for target in google_tcmalloc.BAZEL_TARGETS)
        ):
            mismatches.append("bazel_action_graph:command")
    compiler_identity = provenance.get("host_compiler_identity")
    if not isinstance(compiler_identity, dict) or not compiler_identity:
        mismatches.append("host_compiler_identity")

    binary = args.google_tcmalloc_binary.expanduser().resolve()
    control_binary = args.google_tcmalloc_control_binary.expanduser().resolve()
    verification = provenance.get("verification")
    if not isinstance(verification, dict):
        mismatches.append("verification")
    else:
        required_verification = {
            "binary": str(binary),
            "binary_sha256": sha256_file(binary) if binary.is_file() else None,
            "strong_malloc_symbol": True,
            "hpaa_symbols_present": True,
            "dynamic_gperftools_dependency": False,
        }
        for key, value in required_verification.items():
            if verification.get(key) != value:
                mismatches.append(f"verification:{key}")
        if not isinstance(verification.get("ldd"), list):
            mismatches.append("verification:ldd")
        if not isinstance(verification.get("smoke_command"), list) or verification.get(
            "smoke_checksum"
        ) is None:
            mismatches.append("verification:smoke")
        if not mismatches:
            try:
                fresh_verification = google_tcmalloc.verify_binary(binary)
            except google_tcmalloc.BuildBlocked as error:
                mismatches.append(f"fresh_binary_verification:{error}")
            else:
                if fresh_verification["binary_sha256"] != verification["binary_sha256"]:
                    mismatches.append("fresh_binary_sha256")
    control_verification = provenance.get("control_verification")
    if not isinstance(control_verification, dict):
        mismatches.append("control_verification")
    else:
        required_control_verification = {
            "binary": str(control_binary),
            "binary_sha256": (
                sha256_file(control_binary) if control_binary.is_file() else None
            ),
            "strong_malloc_symbol": False,
            "hpaa_symbols_present": False,
            "dynamic_gperftools_dependency": False,
        }
        for key, value in required_control_verification.items():
            if control_verification.get(key) != value:
                mismatches.append(f"control_verification:{key}")
        if not isinstance(control_verification.get("ldd"), list):
            mismatches.append("control_verification:ldd")
        if not isinstance(control_verification.get("smoke_command"), list) or control_verification.get(
            "smoke_checksum"
        ) is None:
            mismatches.append("control_verification:smoke")
        if (
            isinstance(verification, dict)
            and verification.get("smoke_checksum")
            != control_verification.get("smoke_checksum")
        ):
            mismatches.append("control_trace_checksum")
        if not mismatches:
            try:
                fresh_control = google_tcmalloc.verify_control_binary(control_binary)
            except google_tcmalloc.BuildBlocked as error:
                mismatches.append(f"fresh_control_verification:{error}")
            else:
                if fresh_control["binary_sha256"] != control_verification["binary_sha256"]:
                    mismatches.append("fresh_control_sha256")
    if mismatches:
        raise RuntimeError(
            "Google TCMalloc/Temeraire arm BLOCKED: invalid fixed-revision Bazel "
            "provenance: "
            + ", ".join(mismatches)
        )
    provenance["provenance_path"] = str(provenance_path)
    provenance["binary"] = str(binary)
    provenance["control_binary"] = str(control_binary)
    return provenance


def validate_memfs_prefix(prefix: Path | None) -> Path | None:
    if prefix is None:
        return None
    resolved = prefix.expanduser().resolve()
    if not resolved.parent.is_dir() or not os.access(resolved.parent, os.W_OK):
        raise RuntimeError(f"TCMalloc memfs prefix parent is not writable: {resolved.parent}")
    filesystem = subprocess.run(
        ["stat", "-f", "-c", "%T", str(resolved.parent)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    if filesystem != "hugetlbfs":
        raise RuntimeError(f"TCMalloc memfs prefix must be on hugetlbfs, got {filesystem}")
    return resolved


def build_probes(
    args: argparse.Namespace, google_tcmalloc_record: dict[str, Any]
) -> list[dict[str, Any]]:
    commands = [
        [
            "cargo",
            "build",
            "--release",
            "-p",
            "unialloc",
            "--example",
            "lifetime_hugepage_allocator_probe",
            "--features",
            "lifetime_hugepage",
        ],
    ]
    if not args.skip_build:
        for command in commands:
            subprocess.run(command, cwd=ROOT, check=True)
    if not SEMANTIC_BINARY.is_file():
        raise RuntimeError(f"missing probe binary: {SEMANTIC_BINARY}")
    builds = [
        {
            "command": command,
            "binary": str(SEMANTIC_BINARY.relative_to(ROOT)),
            "binary_sha256": sha256_file(SEMANTIC_BINARY),
        }
        for command in commands
    ]
    builds.append(
        {
            "command": google_tcmalloc_record.get("build_command"),
            "binary": google_tcmalloc_record["binary"],
            "binary_sha256": google_tcmalloc_record["verification"]["binary_sha256"],
            "allocator": "Google TCMalloc",
            "hugepage_allocator": "Temeraire / HPAA",
            "malloc_target": google_tcmalloc_record["malloc_target"],
            "commit": google_tcmalloc_record["commit"],
        }
    )
    builds.append(
        {
            "command": google_tcmalloc_record.get("build_command"),
            "binary": google_tcmalloc_record["control_binary"],
            "binary_sha256": google_tcmalloc_record["control_verification"][
                "binary_sha256"
            ],
            "allocator": "System allocator control",
            "bazel_target": google_tcmalloc_record["bazel_control_target"],
            "matched_codegen_control_for": "Google TCMalloc / Temeraire HPAA",
        }
    )
    return builds


def pinned(command: list[str], args: argparse.Namespace) -> list[str]:
    return [
        "numactl",
        f"--physcpubind={args.cpu}",
        f"--membind={args.numa_node}",
        *command,
    ]


def semantic_command(
    case: SemanticCase, args: argparse.Namespace, repeat_seed: int
) -> list[str]:
    command = [
        str(SEMANTIC_BINARY),
        "--policy",
        case.policy,
        "--identity-mode",
        "exact",
        "--types-per-truth",
        str(args.types_per_truth),
        "--objects",
        str(args.objects),
        "--slot-bytes",
        str(args.slot_bytes),
        "--long-fraction",
        "0.5",
        "--false-long-rate",
        str(args.false_long_rate),
        "--false-short-rate",
        str(args.false_short_rate),
        "--confidence-threshold",
        "0",
        "--correct-confidence",
        "95",
        "--error-confidence",
        "40",
        "--confidence-overlap-rate",
        "0",
        "--unknown-rate",
        "0",
        "--ephemeral-waves",
        str(args.waves),
        "--warmup-passes",
        str(args.warmup_passes),
        "--passes",
        str(args.passes),
        "--seed",
        str(repeat_seed),
    ]
    if case.disable_process_thp:
        command.extend(("--disable-process-thp", "--require-no-thp"))
    elif case.expected_backing == "thp":
        command.append("--require-thp")
    elif case.expected_backing == "ordinary-no-thp":
        command.append("--require-no-thp")
    return pinned(command, args)


def neutral_command(
    case: NeutralCase,
    args: argparse.Namespace,
    repeat_seed: int,
    google_tcmalloc_binary: Path,
    bazel_control_binary: Path,
) -> list[str]:
    binary = (
        google_tcmalloc_binary
        if case.name == "google_tcmalloc_temeraire"
        else bazel_control_binary
    )
    return pinned(
        [
            str(binary),
            "--objects",
            str(args.objects),
            "--slot-bytes",
            str(args.slot_bytes),
            "--passes",
            str(args.passes),
            "--warmup-passes",
            str(args.warmup_passes),
            "--waves",
            str(args.waves),
            "--settle-ms",
            str(args.settle_ms),
            "--seed",
            str(repeat_seed),
        ],
        args,
    )


def clean_allocator_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in SANITIZED_ENV_KEYS
        and not any(key.startswith(prefix) for prefix in SANITIZED_ENV_PREFIXES)
    }


def neutral_environment(
    case: NeutralCase,
    libraries: dict[str, dict[str, Any]],
    memfs_prefix: Path | None,
) -> tuple[dict[str, str], dict[str, str]]:
    env = clean_allocator_environment()
    explicit = dict(case.environment)
    if case.library_key is not None:
        library = Path(libraries[case.library_key]["path"])
        explicit["LD_PRELOAD"] = str(library)
        explicit["UNIALLOC_EVAL_PRELOAD_TOKEN"] = library.name
    if case.name == "gperftools_legacy_hugetlb":
        if memfs_prefix is None:
            raise RuntimeError(
                "gperftools-legacy HugeTLB requested without a hugetlbfs prefix"
            )
        explicit["TCMALLOC_MEMFS_MALLOC_PATH"] = str(memfs_prefix)
    if case.disable_process_thp:
        explicit["UNIALLOC_EVAL_THP_DISABLE"] = "1"
    env.update(explicit)
    return env, explicit


def parse_single_json(stdout: str) -> dict[str, Any]:
    rows = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if len(rows) != 1:
        raise RuntimeError(f"expected one JSON row, got {len(rows)}")
    return json.loads(rows[0])


def validate_neutral_row(
    row: dict[str, Any], case: NeutralCase, min_thp_coverage: float
) -> list[str]:
    failures: list[str] = []
    if row.get("source") != "cross_allocator_large_page_workload":
        failures.append("source")
    if row.get("passed") is not True:
        failures.append("probe_failed")
    if row.get("host_thp_mode") != "madvise":
        failures.append("host_thp_mode")
    if row.get("memory_evidence_complete") is not True:
        failures.append("memory_evidence")
    if case.library_key is not None and row.get("preload_token_observed") is not True:
        failures.append("preload_not_observed")
    anon_huge = int(row.get("anon_huge_delta_kib", -1))
    hugetlb = int(row.get("hugetlb_delta_kib", -1))
    if case.expected_backing == "thp":
        if anon_huge <= 0:
            failures.append("missing_anon_thp")
        if float(row.get("peak_thp_coverage", 0)) < min_thp_coverage:
            failures.append("thp_coverage")
        if hugetlb != 0:
            failures.append("unexpected_hugetlb")
    elif case.expected_backing == "no-thp":
        if anon_huge != 0:
            failures.append("unexpected_anon_thp")
    elif case.expected_backing == "hugetlb":
        if hugetlb <= 0:
            failures.append("missing_hugetlb")
        if anon_huge != 0:
            failures.append("unexpected_anon_thp")
    if case.disable_process_thp and int(row.get("process_thp_disabled", 0)) != 1:
        failures.append("process_thp_disable")
    if case.name == "mimalloc_thp_off" and int(row.get("process_thp_disabled", 0)) != 1:
        failures.append("mimalloc_process_thp_disable")
    return failures


def run_process(
    command: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr[-4000:]}"
        )
    return completed


def semantic_topology_failures(rows: dict[str, dict[str, Any]]) -> list[str]:
    on = rows["unialloc_lifetime_thp_on"]
    off = rows["unialloc_lifetime_thp_off"]
    failures: list[str] = []
    for field in (
        "prediction_trace_digest",
        "routed_allocations",
        "routed_deallocations",
        "thp_extent_mappings",
        "ordinary_extent_mappings",
        "identity_region_assignments",
        "slot_bump_allocations",
        "slot_reuse_hits",
    ):
        if on.get(field) != off.get(field):
            failures.append(f"same_policy_topology:{field}")
    if off.get("process_thp_disable_requested") is not True:
        failures.append("forced_off_request")
    if off.get("process_thp_disabled") is not True:
        failures.append("forced_off_status")
    if int(off.get("observed_anon_huge_delta_kib", -1)) != 0:
        failures.append("forced_off_backing")
    return failures


def run_block(
    *,
    block: int,
    measured: bool,
    args: argparse.Namespace,
    libraries: dict[str, dict[str, Any]],
    memfs_prefix: Path | None,
    google_tcmalloc_binary: Path,
    bazel_control_binary: Path,
    logs_dir: Path,
    randomizer: random.Random,
) -> list[dict[str, Any]]:
    repeat_seed = args.seed + block * 1_000_003
    rows: list[dict[str, Any]] = []
    semantic_order = list(SEMANTIC_CASES)
    randomizer.shuffle(semantic_order)
    semantic_rows: dict[str, dict[str, Any]] = {}
    for order_index, case in enumerate(semantic_order):
        prefix = f"block-{block:03d}-semantic-{order_index:02d}-{case.name}"
        completed = run_process(
            semantic_command(case, args, repeat_seed),
            env=clean_allocator_environment(),
            timeout=args.timeout,
            stdout_path=logs_dir / f"{prefix}.stdout",
            stderr_path=logs_dir / f"{prefix}.stderr",
        )
        row = lifetime_thp.parse_probe(
            completed.stdout,
            case.expected_backing,
            min_steady_thp_coverage=args.min_semantic_thp_coverage,
        )
        row.update(
            {
                "family": "unialloc-semantic",
                "case": case.name,
                "label": case.label,
                "mechanism": case.mechanism,
                "block": block,
                "measured": measured,
                "order_index": order_index,
                "repeat_seed": repeat_seed,
                "command": semantic_command(case, args, repeat_seed),
            }
        )
        semantic_rows[case.name] = row
        rows.append(row)
    topology_failures = semantic_topology_failures(semantic_rows)
    if topology_failures:
        raise RuntimeError(f"semantic block {block} failed: {','.join(topology_failures)}")

    neutral_order = list(neutral_cases(args.include_gperftools_legacy))
    randomizer.shuffle(neutral_order)
    neutral_rows: list[dict[str, Any]] = []
    for order_index, case in enumerate(neutral_order):
        prefix = f"block-{block:03d}-neutral-{order_index:02d}-{case.name}"
        env, explicit_env = neutral_environment(case, libraries, memfs_prefix)
        pool_before = hugepages_free()
        completed = run_process(
            neutral_command(
                case,
                args,
                repeat_seed,
                google_tcmalloc_binary,
                bazel_control_binary,
            ),
            env=env,
            timeout=args.timeout,
            stdout_path=logs_dir / f"{prefix}.stdout",
            stderr_path=logs_dir / f"{prefix}.stderr",
        )
        pool_after = hugepages_free()
        row = parse_single_json(completed.stdout)
        failures = validate_neutral_row(row, case, args.min_neutral_thp_coverage)
        if case.expected_backing == "hugetlb" and pool_before != pool_after:
            failures.append("hugetlb_pool_not_restored")
        row.update(
            {
                "family": "allocator-neutral",
                "case": case.name,
                "label": case.label,
                "mechanism": case.mechanism,
                "expected_backing": case.expected_backing,
                "block": block,
                "measured": measured,
                "order_index": order_index,
                "repeat_seed": repeat_seed,
                "explicit_environment": explicit_env,
                "command": neutral_command(
                    case,
                    args,
                    repeat_seed,
                    google_tcmalloc_binary,
                    bazel_control_binary,
                ),
                "hugepages_free_before": pool_before,
                "hugepages_free_after": pool_after,
                "evidence_failures": failures,
            }
        )
        if failures:
            raise RuntimeError(
                f"neutral block {block}/{case.name} failed: {','.join(failures)}"
            )
        neutral_rows.append(row)
        rows.append(row)
    checksums = {str(row["checksum"]) for row in neutral_rows}
    if len(checksums) != 1:
        raise RuntimeError(f"neutral block {block} checksum mismatch: {checksums}")
    return rows


def percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires samples")
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def paired_effect(
    rows_by_case: dict[str, list[dict[str, Any]]],
    *,
    target: str,
    baseline: str,
    field: str,
    seed: int,
    resamples: int,
    equivalence_pct: float,
) -> dict[str, Any]:
    target_by_block = {int(row["block"]): float(row[field]) for row in rows_by_case[target]}
    baseline_by_block = {
        int(row["block"]): float(row[field]) for row in rows_by_case[baseline]
    }
    blocks = sorted(set(target_by_block) & set(baseline_by_block))
    if len(blocks) != len(target_by_block) or len(blocks) != len(baseline_by_block):
        raise RuntimeError(f"incomplete pair {target}/{baseline}")
    log_ratios = [
        math.log(target_by_block[block] / baseline_by_block[block]) for block in blocks
    ]

    def improvement(values: Iterable[float]) -> float:
        values = list(values)
        return (1.0 - math.exp(statistics.fmean(values))) * 100.0

    point = improvement(log_ratios)
    rng = random.Random(seed)
    bootstrap = sorted(
        improvement(log_ratios[rng.randrange(len(log_ratios))] for _ in log_ratios)
        for _ in range(resamples)
    )
    low = percentile(bootstrap, 0.025)
    high = percentile(bootstrap, 0.975)
    if low > 0:
        conclusion = "improved"
    elif high < 0:
        conclusion = "regressed"
    elif low >= -equivalence_pct and high <= equivalence_pct:
        conclusion = "practically-equivalent"
    else:
        conclusion = "inconclusive"
    return {
        "target": target,
        "baseline": baseline,
        "metric": field,
        "sample_count": len(blocks),
        "paired_geomean_ratio": math.exp(statistics.fmean(log_ratios)),
        "improvement_pct": point,
        "ci_low_pct": low,
        "ci_high_pct": high,
        "lower_is_better": True,
        "equivalence_band_pct": equivalence_pct,
        "conclusion": conclusion,
        "block_ratios": [math.exp(value) for value in log_ratios],
    }


def median_summary(rows: list[dict[str, Any]], family: str) -> dict[str, Any]:
    fields = (
        ("ns_per_touch", "median_ns_per_touch"),
        ("max_effective_resident_kib", "median_max_effective_resident_kib"),
    )
    if family == "unialloc-semantic":
        fields += (
            (
                "allocator_lifecycle_ns_per_allocation",
                "median_lifecycle_ns_per_allocation",
            ),
            ("observed_anon_huge_delta_kib", "median_anon_huge_delta_kib"),
            ("peak_hugetlb_kib", "median_hugetlb_delta_kib"),
            ("classification_failure_rate", "median_classification_failure_rate"),
            (
                "policy_intent_placement_failure_rate",
                "median_policy_intent_placement_failure_rate",
            ),
        )
    else:
        fields += (
            ("lifecycle_ns_per_allocation", "median_lifecycle_ns_per_allocation"),
            ("anon_huge_delta_kib", "median_anon_huge_delta_kib"),
            ("hugetlb_delta_kib", "median_hugetlb_delta_kib"),
            ("peak_thp_coverage", "median_peak_thp_coverage"),
        )
    result = {output: statistics.median(float(row[source]) for row in rows) for source, output in fields}
    result.update(
        {
            "case": rows[0]["case"],
            "label": rows[0]["label"],
            "family": family,
            # Normalize historical samples when presentation-only metadata was
            # too broad; raw measured values remain untouched.
            "mechanism": CANONICAL_MECHANISM_BY_CASE.get(
                str(rows[0]["case"]), str(rows[0]["mechanism"])
            ),
            "samples": len(rows),
        }
    )
    return result


def google_tcmalloc_hugepage_claim_gate(
    rows: Sequence[dict[str, Any]], min_thp_coverage: float
) -> dict[str, Any]:
    evidence = []
    for row in rows:
        anon_huge_kib = int(row.get("anon_huge_delta_kib", 0))
        hugetlb_kib = int(row.get("hugetlb_delta_kib", 0))
        coverage = float(row.get("peak_thp_coverage", 0.0))
        realized = hugetlb_kib > 0 or (
            anon_huge_kib > 0 and coverage >= min_thp_coverage
        )
        evidence.append(
            {
                "block": int(row["block"]),
                "anon_huge_kib": anon_huge_kib,
                "hugetlb_kib": hugetlb_kib,
                "peak_thp_coverage": coverage,
                "physical_hugepage_backing_realized": realized,
            }
        )
    excluded = [
        record["block"]
        for record in evidence
        if not record["physical_hugepage_backing_realized"]
    ]
    return {
        "identity_claim_ready": bool(evidence),
        "hugepage_mechanism_claim_ready": bool(evidence) and not excluded,
        "hpaa_performance_causality_claim_ready": False,
        "measured_sample_count": len(evidence),
        "backed_sample_count": len(evidence) - len(excluded),
        "excluded_blocks": excluded,
        "sample_evidence": evidence,
        "claim_boundary": (
            "The fixed-revision link proves Google TCMalloc with HPAA code. "
            "Every measured sample needs physical hugepage backing for a "
            "Temeraire/hugepage-mechanism endpoint claim. A matched HPAA-off "
            "control is still required for HPAA performance causality."
        ),
    }


def summarize(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> dict[str, Any]:
    measured = [row for row in rows if row["measured"]]
    rows_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in measured:
        rows_by_case.setdefault(str(row["case"]), []).append(row)
    case_summaries = {
        case: median_summary(case_rows, str(case_rows[0]["family"]))
        for case, case_rows in rows_by_case.items()
    }
    google_hugepage_gate = google_tcmalloc_hugepage_claim_gate(
        rows_by_case["google_tcmalloc_temeraire"], args.min_neutral_thp_coverage
    )
    pairs = [
        (
            "unialloc_lifetime_thp",
            "unialloc_lifetime_thp_on",
            "unialloc_lifetime_thp_off",
            "UniAlloc selective THP",
            "selective-thp",
        ),
        (
            "mimalloc_thp",
            "mimalloc_thp_on",
            "mimalloc_thp_off",
            "mimalloc global THP",
            "process-wide-thp",
        ),
        (
            "jemalloc_thp",
            "jemalloc_thp_on",
            "jemalloc_thp_off",
            "jemalloc allocator THP",
            "allocator-wide-thp",
        ),
        (
            "snmalloc_os_eligibility",
            "snmalloc_default",
            "snmalloc_os_thp_off",
            "snmalloc OS eligibility",
            "os-eligibility",
        ),
    ]
    if args.include_gperftools_legacy:
        pairs.append(
            (
                "gperftools_legacy_hugetlb",
                "gperftools_legacy_hugetlb",
                "gperftools_legacy_default",
                "gperftools-legacy HugeTLB",
                "legacy-explicit-hugetlb",
            )
        )
    comparisons: dict[str, dict[str, Any]] = {}
    toggle_effects: list[dict[str, Any]] = []
    for index, (name, target, baseline, label, mechanism) in enumerate(pairs):
        touch = paired_effect(
            rows_by_case,
            target=target,
            baseline=baseline,
            field="ns_per_touch",
            seed=args.seed + index * 11,
            resamples=args.bootstrap_resamples,
            equivalence_pct=3.0,
        )
        memory = paired_effect(
            rows_by_case,
            target=target,
            baseline=baseline,
            field="max_effective_resident_kib",
            seed=args.seed + index * 11 + 1,
            resamples=args.bootstrap_resamples,
            equivalence_pct=5.0,
        )
        comparisons[name] = {"touch": touch, "max_effective_resident": memory}
        toggle_effects.append(
            {
                "label": label,
                "mechanism": mechanism,
                "speedup_pct": touch["improvement_pct"],
                "speedup_ci_low_pct": touch["ci_low_pct"],
                "speedup_ci_high_pct": touch["ci_high_pct"],
                "speedup_conclusion": touch["conclusion"],
                "max_resident_change_pct": -memory["improvement_pct"],
                "max_resident_ci_low_pct": -memory["ci_high_pct"],
                "max_resident_ci_high_pct": -memory["ci_low_pct"],
                "max_resident_conclusion": memory["conclusion"],
            }
        )

    endpoint_names = [
        "glibc_default",
        "google_tcmalloc_temeraire",
        "mimalloc_thp_off",
        "mimalloc_thp_on",
        "jemalloc_thp_off",
        "jemalloc_thp_on",
        "snmalloc_default",
        "snmalloc_os_thp_off",
    ]
    if args.include_gperftools_legacy:
        endpoint_names.extend(
            ("gperftools_legacy_default", "gperftools_legacy_hugetlb")
        )
    pair_for = {
        "mimalloc_thp_off": "mimalloc",
        "mimalloc_thp_on": "mimalloc",
        "jemalloc_thp_off": "jemalloc",
        "jemalloc_thp_on": "jemalloc",
        "google_tcmalloc_temeraire": "google-tcmalloc",
        "gperftools_legacy_default": "gperftools-legacy",
        "gperftools_legacy_hugetlb": "gperftools-legacy",
        "snmalloc_default": "snmalloc",
        "snmalloc_os_thp_off": "snmalloc",
        "glibc_default": "glibc",
    }
    mode_for = {
        "glibc_default": "default",
        "google_tcmalloc_temeraire": "default",
        "mimalloc_thp_off": "off",
        "mimalloc_thp_on": "on",
        "jemalloc_thp_off": "off",
        "jemalloc_thp_on": "on",
        "gperftools_legacy_default": "off",
        "gperftools_legacy_hugetlb": "on",
        "snmalloc_default": "on",
        "snmalloc_os_thp_off": "off",
    }
    endpoint_label_for = {
        "glibc_default": "glibc",
        "google_tcmalloc_temeraire": "Google TCMalloc / Temeraire",
        "mimalloc_thp_off": "mimalloc off",
        "mimalloc_thp_on": "mimalloc THP",
        "jemalloc_thp_off": "jemalloc off",
        "jemalloc_thp_on": "jemalloc THP",
        "gperftools_legacy_default": "gperftools-legacy anon",
        "gperftools_legacy_hugetlb": "gperftools-legacy HugeTLB",
        "snmalloc_default": "snmalloc eligible",
        "snmalloc_os_thp_off": "snmalloc disabled",
    }
    endpoints = []
    for case in endpoint_names:
        summary = case_summaries[case]
        endpoints.append(
            {
                "label": endpoint_label_for[case],
                "mechanism": summary["mechanism"],
                "ns_per_touch": summary["median_ns_per_touch"],
                "max_effective_resident_mib": summary[
                    "median_max_effective_resident_kib"
                ]
                / 1024,
                "large_page_backing_mib": (
                    summary["median_anon_huge_delta_kib"]
                    + summary["median_hugetlb_delta_kib"]
                )
                / 1024,
                "pair": pair_for[case],
                "mode": mode_for[case],
            }
        )
    backing_names = [
        "unialloc_lifetime_thp_off",
        "unialloc_lifetime_thp_on",
        "google_tcmalloc_temeraire",
        "mimalloc_thp_off",
        "mimalloc_thp_on",
        "jemalloc_thp_off",
        "jemalloc_thp_on",
        "snmalloc_default",
    ]
    if args.include_gperftools_legacy:
        backing_names.extend(
            ("gperftools_legacy_default", "gperftools_legacy_hugetlb")
        )
    backing_label_for = {
        "unialloc_lifetime_thp_off": "UniAlloc off",
        "unialloc_lifetime_thp_on": "UniAlloc selective THP",
        "google_tcmalloc_temeraire": "Google TCMalloc / Temeraire",
        "mimalloc_thp_off": "mimalloc off",
        "mimalloc_thp_on": "mimalloc THP",
        "jemalloc_thp_off": "jemalloc off",
        "jemalloc_thp_on": "jemalloc THP",
        "gperftools_legacy_default": "gperftools-legacy anon",
        "gperftools_legacy_hugetlb": "gperftools-legacy HugeTLB",
        "snmalloc_default": "snmalloc default",
    }
    backing = [
        {
            "label": backing_label_for[case],
            "anon_thp_mib": case_summaries[case]["median_anon_huge_delta_kib"] / 1024,
            "hugetlb_mib": case_summaries[case]["median_hugetlb_delta_kib"] / 1024,
        }
        for case in backing_names
    ]
    return {
        "schema_version": 1,
        "source": "cross_allocator_large_page_experiment",
        "claim_grade": True,
        "case_summaries": case_summaries,
        "google_tcmalloc_temeraire_gate": google_hugepage_gate,
        "comparisons": comparisons,
        "slide_data": {
            "toggle_effects": toggle_effects,
            "endpoints": endpoints,
            "backing": backing,
        },
        "evidence_contract": {
            "semantic_primary_pair": (
                "same long-thp policy and trace; PR_SET_THP_DISABLE changes only "
                "physical THP eligibility"
            ),
            "neutral_pairing": "same malloc/free binary and trace within each allocator",
            "actual_thp_backing": "/proc/self/smaps_rollup AnonHugePages",
            "actual_hugetlb_backing": "/proc/self/status HugetlbPages",
            "resident_metric": "max(VmRSS + HugetlbPages) across live phases",
            "cross_family_raw_timing_ranking_allowed": False,
            "gperftools_is_google_tcmalloc": False,
            "google_tcmalloc_identity": "fixed-revision Bazel link-time Temeraire/HPAA",
            "google_tcmalloc_fallback_allowed": False,
            "google_tcmalloc_hugepage_claim_ready": google_hugepage_gate[
                "hugepage_mechanism_claim_ready"
            ],
            "google_tcmalloc_hpaa_performance_causality_claim_ready": False,
            "gperftools_legacy_claim_role": "historical-only",
            "snmalloc_control_kind": "OS eligibility; snmalloc has no public THP toggle",
        },
        "invariants_passed": True,
    }


def write_summary_csv(summary: dict[str, Any], path: Path) -> None:
    fields = (
        "case",
        "label",
        "family",
        "mechanism",
        "samples",
        "median_ns_per_touch",
        "median_lifecycle_ns_per_allocation",
        "median_max_effective_resident_kib",
        "median_anon_huge_delta_kib",
        "median_hugetlb_delta_kib",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        for case in sorted(summary["case_summaries"]):
            writer.writerow(summary["case_summaries"][case])


def write_toggle_csv(summary: dict[str, Any], path: Path) -> None:
    rows = summary["slide_data"]["toggle_effects"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=tuple(rows[0]), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def write_manifest(directory: Path, *, exclude: set[str] | None = None) -> None:
    excluded = exclude or {"manifest.json"}
    files = []
    for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
        relative = str(path.relative_to(directory))
        if relative in excluded:
            continue
        files.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    (directory / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "files": files}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    logs_dir = output_dir / "raw"
    logs_dir.mkdir()
    (output_dir / "host-before.txt").write_text(host_snapshot(), encoding="utf-8")
    try:
        google_tcmalloc_record = google_tcmalloc_provenance(args)
    except (RuntimeError, OSError, json.JSONDecodeError) as error:
        source_provenance = args.google_tcmalloc_provenance.expanduser().resolve()
        copied_provenance = None
        if source_provenance.is_file():
            copied_provenance = output_dir / "google-tcmalloc-provenance.json"
            shutil.copy2(source_provenance, copied_provenance)
        blocked = {
            "schema_version": 1,
            "source": "cross_allocator_large_page_experiment",
            "status": "BLOCKED",
            "claim_grade": False,
            "blocked_arm": "google_tcmalloc_temeraire",
            "reason": str(error),
            "fallback_allowed": False,
            "fallback_attempted": False,
            "gperftools_legacy_eligible_as_google_tcmalloc": False,
            "google_tcmalloc_commit": google_tcmalloc.GOOGLE_TCMALLOC_COMMIT,
            "malloc_target": google_tcmalloc.MALLOC_TARGET,
            "requested_build_root": str(args.google_tcmalloc_build_root),
            "source_provenance": str(source_provenance),
            "copied_provenance": (
                copied_provenance.name if copied_provenance is not None else None
            ),
        }
        (output_dir / "runner-manifest.json").write_text(
            json.dumps(blocked, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "host-after.txt").write_text(host_snapshot(), encoding="utf-8")
        write_manifest(output_dir)
        print(f"Google TCMalloc/Temeraire arm BLOCKED: {error}", file=sys.stderr)
        print(output_dir / "runner-manifest.json")
        return 2
    google_tcmalloc_binary = Path(google_tcmalloc_record["binary"])
    bazel_control_binary = Path(google_tcmalloc_record["control_binary"])
    libraries = library_provenance(args)
    memfs_prefix = validate_memfs_prefix(args.gperftools_legacy_memfs_prefix)
    if args.include_gperftools_legacy and memfs_prefix is None:
        raise RuntimeError(
            "--include-gperftools-legacy requires "
            "--gperftools-legacy-memfs-prefix for its historical HugeTLB arm"
        )
    builds = build_probes(args, google_tcmalloc_record)
    runner_manifest = {
        "schema_version": 1,
        "argv": [sys.executable, str(Path(__file__).resolve()), *(argv or sys.argv[1:])],
        "git_head": git_text("rev-parse", "HEAD"),
        "git_status": git_text("status", "--short"),
        "cpu": args.cpu,
        "numa_node": args.numa_node,
        "repeats": args.repeats,
        "warmup_blocks": 0 if args.skip_warmup else args.warmup_blocks,
        "bootstrap_resamples": args.bootstrap_resamples,
        "seed": args.seed,
        "objects": args.objects,
        "slot_bytes": args.slot_bytes,
        "passes": args.passes,
        "warmup_passes": args.warmup_passes,
        "waves": args.waves,
        "settle_ms": args.settle_ms,
        "min_semantic_thp_coverage": args.min_semantic_thp_coverage,
        "min_neutral_thp_coverage": args.min_neutral_thp_coverage,
        "libraries": libraries,
        "google_tcmalloc_temeraire": google_tcmalloc_record,
        "gperftools_legacy_included": args.include_gperftools_legacy,
        "gperftools_legacy_memfs_prefix": (
            str(memfs_prefix) if memfs_prefix is not None else None
        ),
        "probe_builds": builds,
        "neutral_source_sha256": sha256_file(NEUTRAL_SOURCE),
        "semantic_source_sha256": sha256_file(
            ROOT / "unialloc/examples/lifetime_hugepage_allocator_probe.rs"
        ),
    }
    (output_dir / "runner-manifest.json").write_text(
        json.dumps(runner_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    rows: list[dict[str, Any]] = []
    randomizer = random.Random(args.seed)
    warmups = 0 if args.skip_warmup else args.warmup_blocks
    total_blocks = warmups + args.repeats
    samples_path = output_dir / "samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as samples:
        for block in range(total_blocks):
            measured = block >= warmups
            block_rows = run_block(
                block=block,
                measured=measured,
                args=args,
                libraries=libraries,
                memfs_prefix=memfs_prefix,
                google_tcmalloc_binary=google_tcmalloc_binary,
                bazel_control_binary=bazel_control_binary,
                logs_dir=logs_dir,
                randomizer=randomizer,
            )
            rows.extend(block_rows)
            for row in block_rows:
                samples.write(json.dumps(row, sort_keys=True) + "\n")
            samples.flush()
            print(
                f"block={block} measured={measured} "
                f"samples={len(block_rows)}",
                flush=True,
            )

    summary = summarize(rows, args)
    summary.update(
        {
            "run_id": output_dir.name,
            "measured_repeats": args.repeats,
            "warmup_blocks": warmups,
            "objects": args.objects,
            "slot_bytes": args.slot_bytes,
            "passes": args.passes,
            "waves": args.waves,
            "seed": args.seed,
        }
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_summary_csv(summary, output_dir / "summary.csv")
    write_toggle_csv(summary, output_dir / "toggle-effects.csv")
    (output_dir / "host-after.txt").write_text(host_snapshot(), encoding="utf-8")
    write_manifest(output_dir)
    print(output_dir / "summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
