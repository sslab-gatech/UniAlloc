#!/usr/bin/env python3
"""Build the neutral probe with pinned Google TCMalloc/Temeraire via Bazel.

The helper deliberately fails closed.  A missing Bazel executable, checkout
revision mismatch, build failure, or link-time proof failure produces a
``BLOCKED`` provenance record.  It never substitutes gperftools or LD_PRELOAD.

Google's supported integration is a Bazel ``cc_binary`` ``malloc`` target:
https://google.github.io/tcmalloc/quickstart.html#creating-your-build-file
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import google_tcmalloc_support as support


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "evaluation/probes/cross_allocator_large_page_workload.c"
DEFAULT_BUILD_ROOT = ROOT / "evaluation/raw/google-tcmalloc-temeraire-build"
GOOGLE_TCMALLOC_REPOSITORY = support.UPSTREAM_URL
# Published as Bazel Central Registry module 0.0.0-20250927-12f2552.
GOOGLE_TCMALLOC_COMMIT = support.UPSTREAM_REVISION
GOOGLE_TCMALLOC_COMMIT_DATE = support.UPSTREAM_DATE
REQUIRED_BAZEL_VERSION = support.BAZEL_VERSION
GOOGLE_TCMALLOC_MODULE_VERSION = "0.0.0-20250927-12f2552"
RULES_CC_MODULE_VERSION = "0.1.5"
BAZEL_TARGET = "//:cross_allocator_large_page_workload_google_tcmalloc"
BAZEL_CONTROL_TARGET = "//:cross_allocator_large_page_workload_system"
BAZEL_TARGETS = (BAZEL_CONTROL_TARGET, BAZEL_TARGET)
MALLOC_TARGET = "@com_google_tcmalloc//tcmalloc"
IMPLEMENTATION_FILES = (
    "tcmalloc/page_allocator.cc",
    "tcmalloc/huge_page_aware_allocator.cc",
    "tcmalloc/huge_page_aware_allocator.h",
    "tcmalloc/huge_page_filler.h",
    "tcmalloc/huge_page_subrelease.h",
)
BUILD_OPTIONS = (
    "--compilation_mode=opt",
    "--cxxopt=-std=c++17",
    "--strip=never",
)
PROBE_COPTS = (
    "-std=c11",
    "-O3",
    "-DNDEBUG",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-fno-builtin-malloc",
)


class BuildBlocked(RuntimeError):
    """A required identity, build, or verification condition is unavailable."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
        env=env,
    )


def base_provenance() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "BLOCKED",
        "claim_eligible": False,
        "fallback_allowed": False,
        "gperftools_legacy_eligible_as_google_tcmalloc": False,
        "allocator_identity": "Google TCMalloc",
        "hugepage_allocator_identity": "Temeraire / HugePageAwareAllocator (HPAA)",
        "repository": GOOGLE_TCMALLOC_REPOSITORY,
        "commit": GOOGLE_TCMALLOC_COMMIT,
        "commit_date": GOOGLE_TCMALLOC_COMMIT_DATE,
        "pin_policy": "tested 2025 compatibility pin; upstream-current tracking is separate",
        "revision_role": "compatibility-pin-not-latest",
        "required_bazel_version": REQUIRED_BAZEL_VERSION,
        "pinned_module_release": GOOGLE_TCMALLOC_MODULE_VERSION,
        "rules_cc_module_version": RULES_CC_MODULE_VERSION,
        "bazel_target": BAZEL_TARGET,
        "bazel_control_target": BAZEL_CONTROL_TARGET,
        "bazel_targets": list(BAZEL_TARGETS),
        "malloc_target": MALLOC_TARGET,
        "build_options": list(BUILD_OPTIONS),
        "probe_copts": list(PROBE_COPTS),
        "official_integration": "Bazel cc_binary malloc attribute at link time",
        "implementation_files": list(IMPLEMENTATION_FILES),
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": None,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "blocked_reasons": [],
    }


def require_bazel(executable: str) -> tuple[str, str, dict[str, str]]:
    resolved = shutil.which(executable)
    if resolved is None:
        raise BuildBlocked(f"Bazel executable unavailable: {executable}")
    bazel_env = dict(os.environ)
    bazel_env["USE_BAZEL_VERSION"] = REQUIRED_BAZEL_VERSION
    version = run([resolved, "--version"], env=bazel_env).stdout.strip()
    if version != f"bazel {REQUIRED_BAZEL_VERSION}":
        raise BuildBlocked(f"unexpected Bazel identity: {version!r}")
    return resolved, version, bazel_env


def prepare_checkout(checkout: Path) -> dict[str, Any]:
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if not (checkout / ".git").is_dir():
        if checkout.exists():
            shutil.rmtree(checkout)
        run(["git", "init", "--quiet", str(checkout)])
        run(
            [
                "git",
                "-C",
                str(checkout),
                "remote",
                "add",
                "origin",
                GOOGLE_TCMALLOC_REPOSITORY,
            ]
        )
    remotes = run(["git", "-C", str(checkout), "remote", "get-url", "origin"]).stdout.strip()
    if remotes != GOOGLE_TCMALLOC_REPOSITORY:
        raise BuildBlocked(f"Google TCMalloc remote mismatch: {remotes}")
    run(
        [
            "git",
            "-C",
            str(checkout),
            "fetch",
            "--depth=1",
            "origin",
            GOOGLE_TCMALLOC_COMMIT,
        ]
    )
    run(["git", "-C", str(checkout), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
    observed = run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
    if observed != GOOGLE_TCMALLOC_COMMIT:
        raise BuildBlocked(
            f"Google TCMalloc revision mismatch: expected {GOOGLE_TCMALLOC_COMMIT}, got {observed}"
        )
    status = run(["git", "-C", str(checkout), "status", "--porcelain"]).stdout.strip()
    if status:
        raise BuildBlocked("Google TCMalloc checkout is dirty")

    identities = []
    for relative in IMPLEMENTATION_FILES:
        path = checkout / relative
        if not path.is_file():
            raise BuildBlocked(f"missing Temeraire/HPAA implementation file: {relative}")
        identities.append({"path": relative, "sha256": sha256_file(path)})
    page_allocator = (checkout / "tcmalloc/page_allocator.cc").read_text(encoding="utf-8")
    if "HugePageAwareAllocator" not in page_allocator:
        raise BuildBlocked("pinned TCMalloc page allocator does not instantiate HPAA")
    commit_record = run(
        [
            "git",
            "-C",
            str(checkout),
            "show",
            "-s",
            "--format=%H%n%cI%n%s",
            "HEAD",
        ]
    ).stdout.splitlines()
    return {
        "commit": commit_record[0],
        "commit_time": commit_record[1],
        "commit_subject": commit_record[2],
        "implementation_file_hashes": identities,
    }


def write_workspace(workspace: Path, checkout: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, workspace / SOURCE.name)
    # local_path_override preserves the exact checked-out commit while Bazel
    # resolves the upstream module's declared dependencies.
    (workspace / "MODULE.bazel").write_text(
        "\n".join(
            (
                'module(name = "unialloc_google_tcmalloc_probe", version = "0")',
                'bazel_dep(name = "tcmalloc", version = "0", repo_name = "com_google_tcmalloc")',
                f'bazel_dep(name = "rules_cc", version = "{RULES_CC_MODULE_VERSION}")',
                "local_path_override(",
                '    module_name = "tcmalloc",',
                f"    path = {json.dumps(str(checkout.resolve()))},",
                ")",
                "",
            )
        ),
        encoding="utf-8",
    )
    (workspace / "BUILD.bazel").write_text(
        "\n".join(
            (
                'load("@rules_cc//cc:cc_binary.bzl", "cc_binary")',
                "",
                "cc_binary(",
                '    name = "cross_allocator_large_page_workload_system",',
                f"    srcs = [{json.dumps(SOURCE.name)}],",
                f"    copts = {json.dumps(list(PROBE_COPTS))},",
                ")",
                "",
                "cc_binary(",
                '    name = "cross_allocator_large_page_workload_google_tcmalloc",',
                f"    srcs = [{json.dumps(SOURCE.name)}],",
                f"    copts = {json.dumps(list(PROBE_COPTS))},",
                f"    malloc = {json.dumps(MALLOC_TARGET)},",
                ")",
                "",
            )
        ),
        encoding="utf-8",
    )


def verify_binary(binary: Path) -> dict[str, Any]:
    if not binary.is_file():
        raise BuildBlocked(f"Bazel output missing: {binary}")
    nm = run(["nm", "-C", str(binary)]).stdout
    if re.search(r"\s[Tt]\s+malloc$", nm, flags=re.MULTILINE) is None:
        raise BuildBlocked("linked probe lacks a strong malloc definition")
    if "HugePageAwareAllocator" not in nm:
        raise BuildBlocked("linked probe lacks HugePageAwareAllocator symbols")
    ldd = run(["ldd", str(binary)]).stdout
    if "libtcmalloc" in ldd or "gperftools" in ldd:
        raise BuildBlocked("probe unexpectedly depends on a preload/shared TCMalloc runtime")
    smoke_command = [
        str(binary),
        "--objects",
        "1024",
        "--passes",
        "1",
        "--warmup-passes",
        "1",
        "--waves",
        "1",
        "--settle-ms",
        "0",
        "--seed",
        "20260715",
    ]
    completed = run(smoke_command)
    rows = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    if len(rows) != 1:
        raise BuildBlocked(f"Google TCMalloc smoke emitted {len(rows)} JSON rows")
    row = json.loads(rows[0])
    if row.get("source") != "cross_allocator_large_page_workload" or row.get("passed") is not True:
        raise BuildBlocked("Google TCMalloc probe smoke failed")
    return {
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "strong_malloc_symbol": True,
        "hpaa_symbols_present": True,
        "dynamic_gperftools_dependency": False,
        "ldd": ldd.splitlines(),
        "smoke_command": smoke_command,
        "smoke_checksum": row.get("checksum"),
    }


def verify_control_binary(binary: Path) -> dict[str, Any]:
    if not binary.is_file():
        raise BuildBlocked(f"Bazel control output missing: {binary}")
    nm = run(["nm", "-C", str(binary)]).stdout
    if re.search(r"\s[Tt]\s+malloc$", nm, flags=re.MULTILINE) is not None:
        raise BuildBlocked("Bazel system control unexpectedly defines malloc")
    if "HugePageAwareAllocator" in nm:
        raise BuildBlocked("Bazel system control unexpectedly contains HPAA symbols")
    ldd = run(["ldd", str(binary)]).stdout
    if "libtcmalloc" in ldd or "gperftools" in ldd:
        raise BuildBlocked("Bazel system control depends on a TCMalloc runtime")
    smoke_command = [
        str(binary),
        "--objects",
        "1024",
        "--passes",
        "1",
        "--warmup-passes",
        "1",
        "--waves",
        "1",
        "--settle-ms",
        "0",
        "--seed",
        "20260715",
    ]
    completed = run(smoke_command)
    rows = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    if len(rows) != 1:
        raise BuildBlocked(f"Bazel system control smoke emitted {len(rows)} JSON rows")
    row = json.loads(rows[0])
    if row.get("source") != "cross_allocator_large_page_workload" or row.get("passed") is not True:
        raise BuildBlocked("Bazel system control smoke failed")
    return {
        "binary": str(binary.resolve()),
        "binary_sha256": sha256_file(binary),
        "strong_malloc_symbol": False,
        "hpaa_symbols_present": False,
        "dynamic_gperftools_dependency": False,
        "ldd": ldd.splitlines(),
        "smoke_command": smoke_command,
        "smoke_checksum": row.get("checksum"),
    }


def replace_executable_artifact(source: Path, destination: Path) -> None:
    """Atomically refresh a Bazel executable even after a read-only prior copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(source, temporary)
        temporary.chmod(temporary.stat().st_mode | 0o111)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def build(build_root: Path, bazel_executable: str) -> dict[str, Any]:
    provenance = base_provenance()
    provenance["build_root"] = str(build_root.resolve())
    try:
        provenance["source_sha256"] = sha256_file(SOURCE)
        bazel, bazel_version, bazel_env = require_bazel(bazel_executable)
        provenance["bazel_executable"] = bazel
        provenance["bazel_version"] = bazel_version
        provenance["bazel_environment"] = {
            "USE_BAZEL_VERSION": REQUIRED_BAZEL_VERSION,
        }
        checkout = build_root / "google-tcmalloc"
        workspace = build_root / "probe-workspace"
        provenance["checkout_path"] = str(checkout.resolve())
        provenance["workspace_path"] = str(workspace.resolve())
        provenance.update(prepare_checkout(checkout))
        write_workspace(workspace, checkout)
        provenance["workspace_files"] = [
            {
                "path": name,
                "sha256": sha256_file(workspace / name),
            }
            for name in ("MODULE.bazel", "BUILD.bazel", SOURCE.name)
        ]
        command = [bazel, "build", *BUILD_OPTIONS, *BAZEL_TARGETS]
        provenance["build_command"] = command
        build_result = run(command, cwd=workspace, check=False, env=bazel_env)
        provenance["build_output_tail"] = build_result.stdout.splitlines()[-100:]
        if build_result.returncode != 0:
            raise BuildBlocked(
                f"Bazel build failed with exit code {build_result.returncode}"
            )
        aquery_expression = (
            'mnemonic("(CppCompile|CppLink)", set('
            + " ".join(BAZEL_TARGETS)
            + "))"
        )
        aquery_command = [
            bazel,
            "aquery",
            "--output=textproto",
            aquery_expression,
        ]
        aquery_result = run(
            aquery_command, cwd=workspace, check=False, env=bazel_env
        )
        provenance["aquery_output_tail"] = aquery_result.stdout.splitlines()[-100:]
        if aquery_result.returncode != 0:
            raise BuildBlocked(
                f"Bazel action-graph capture failed with exit code {aquery_result.returncode}"
            )
        aquery_path = build_root / "evidence/bazel-actions.textproto"
        aquery_path.parent.mkdir(parents=True, exist_ok=True)
        aquery_path.write_text(aquery_result.stdout, encoding="utf-8")
        provenance["bazel_action_graph"] = {
            "command": aquery_command,
            "path": str(aquery_path.resolve()),
            "sha256": sha256_file(aquery_path),
            "bytes": aquery_path.stat().st_size,
            "purpose": "authoritative compile/link action and selected toolchain evidence",
        }
        provenance["host_compiler_identity"] = {
            name: {
                "path": shutil.which(name),
                "version": run([name, "--version"]).stdout.splitlines()[0],
            }
            for name in ("cc", "c++")
            if shutil.which(name) is not None
        }
        bazel_binary = (
            workspace
            / "bazel-bin"
            / "cross_allocator_large_page_workload_google_tcmalloc"
        )
        artifact = build_root / "bin/cross_allocator_large_page_workload_google_tcmalloc"
        control_bazel_binary = (
            workspace / "bazel-bin" / "cross_allocator_large_page_workload_system"
        )
        control_artifact = build_root / "bin/cross_allocator_large_page_workload_system"
        replace_executable_artifact(bazel_binary, artifact)
        replace_executable_artifact(control_bazel_binary, control_artifact)
        provenance["verification"] = verify_binary(artifact)
        provenance["control_verification"] = verify_control_binary(control_artifact)
        if (
            provenance["verification"]["smoke_checksum"]
            != provenance["control_verification"]["smoke_checksum"]
        ):
            raise BuildBlocked("Google TCMalloc and Bazel control traces differ")
        provenance["status"] = "VERIFIED"
        provenance["claim_eligible"] = True
    except (BuildBlocked, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        provenance["blocked_reasons"].append(str(error))
    return provenance


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    parser.add_argument("--bazel", default=os.environ.get("BAZEL", "bazel"))
    parser.add_argument("--provenance", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    build_root = args.build_root.expanduser().resolve()
    build_root.mkdir(parents=True, exist_ok=True)
    provenance_path = (
        args.provenance.expanduser().resolve()
        if args.provenance is not None
        else build_root / "provenance.json"
    )
    provenance = build(build_root, args.bazel)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(provenance_path)
    if provenance["status"] != "VERIFIED":
        print(
            "Google TCMalloc/Temeraire arm BLOCKED: "
            + "; ".join(provenance["blocked_reasons"]),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
