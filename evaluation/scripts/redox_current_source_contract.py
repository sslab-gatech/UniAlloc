#!/usr/bin/env python3
"""Check UniAlloc's current-source Redox compile/codegen/C-ABI contract.

This is a bounded functional regression, not Redox boot or performance evidence.
Final executable linking and execution still require a Redox linker/redoxer/QEMU.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Iterable, Optional, Sequence, Set, Tuple


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_TARGET = "x86_64-unknown-redox"
DEFAULT_FEATURES = "fixed_heap,allow_mem_leak,stats"
# Mirrors the constrained-platform ABI surface audited by evaluate.py.
REQUIRED_C_ABI_SYMBOLS = (
    "unialloc_fixed_heap_try_init",
    "unialloc_fixed_heap_ready",
    "unialloc_fixed_heap_try_extend",
    "unialloc_alloc",
    "unialloc_dealloc",
    "unialloc_realloc",
    "__unialloc_alloc_with_metadata",
    "__unialloc_dealloc_with_metadata",
    "__unialloc_semantic_stats_snapshot_abi_version",
    "__unialloc_semantic_stats_snapshot_size",
    "__unialloc_semantic_stats_snapshot_checked",
    "__unialloc_semantic_stats_snapshot",
    "__unialloc_semantic_stats_reset",
    "__unialloc_semantic_type_stats_snapshot_abi_version",
    "__unialloc_semantic_type_stats_snapshot_record_size",
    "__unialloc_semantic_type_stats_snapshot_checked",
    "__unialloc_semantic_type_stats_snapshot",
    "__unialloc_semantic_fallback_attribution_snapshot_abi_version",
    "__unialloc_semantic_fallback_attribution_snapshot_size",
    "__unialloc_semantic_fallback_attribution_snapshot_checked",
    "__unialloc_semantic_fallback_attribution_snapshot",
    "__unialloc_semantic_metadata_validation_snapshot_abi_version",
    "__unialloc_semantic_metadata_validation_snapshot_size",
    "__unialloc_semantic_metadata_validation_snapshot_checked",
    "__unialloc_semantic_metadata_validation_snapshot",
    "__unialloc_constrained_boot_sample_abi_version",
    "__unialloc_constrained_boot_sample_size",
    "__unialloc_constrained_boot_sample_checked",
)


def run_command(
    label: str,
    command: Sequence[str],
    output_dir: pathlib.Path,
    timeout: int,
) -> Tuple[Dict[str, Any], str]:
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        returncode = proc.returncode
        output = proc.stdout
        error = None
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        error = f"timeout after {timeout}s"
    except OSError as exc:
        returncode = 127
        output = str(exc)
        error = f"could not execute command: {exc}"
    log = output_dir / f"{label}.log"
    log.write_text("$ " + " ".join(command) + "\n" + output, encoding="utf-8", errors="replace")
    return (
        {
            "command": list(command),
            "returncode": returncode,
            "passed": returncode == 0,
            "error": error,
            "log": str(log),
        },
        output,
    )


def elf_x86_64_relocatable_contract(path: pathlib.Path) -> Dict[str, Any]:
    data = path.read_bytes()[:20] if path.exists() else b""
    little_endian = len(data) >= 6 and data[5] == 1
    object_type = int.from_bytes(data[16:18], "little") if little_endian and len(data) >= 18 else None
    machine = int.from_bytes(data[18:20], "little") if little_endian and len(data) >= 20 else None
    passed = (
        data[:4] == b"\x7fELF"
        and len(data) >= 5
        and data[4] == 2
        and little_endian
        and object_type == 1
        and machine == 62
    )
    return {
        "path": str(path),
        "elf_type": object_type,
        "elf_machine": machine,
        "passed": passed,
    }


def symbols_from_nm(text: str) -> Set[str]:
    return {line.split()[-1] for line in text.splitlines() if line.split()}


def symbol_contract(symbols: Iterable[str]) -> Dict[str, Any]:
    present = set(symbols)
    missing = [symbol for symbol in REQUIRED_C_ABI_SYMBOLS if symbol not in present]
    return {"missing": missing, "passed": not missing}


def find_llvm_nm(toolchain: str) -> Optional[pathlib.Path]:
    rustc = shutil.which("rustc") or "rustc"
    try:
        proc = subprocess.run(
            [rustc, f"+{toolchain}", "--print", "sysroot"],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0:
        candidates = sorted(pathlib.Path(proc.stdout.strip()).glob("lib/rustlib/*/bin/llvm-nm"))
        if candidates:
            return candidates[0]
    fallback = shutil.which("llvm-nm")
    return pathlib.Path(fallback) if fallback else None


def git_identity() -> Dict[str, Any]:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True, timeout=30
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=str(ROOT), text=True, timeout=30
        ).splitlines()
        return {"git_head": head, "git_worktree_clean": not status, "git_status": status}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"git_head": None, "git_worktree_clean": False, "error": str(exc)}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", default=(ROOT / "rust-toolchain").read_text().strip())
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--features", default=DEFAULT_FEATURES)
    parser.add_argument("--output-dir")
    parser.add_argument("--timeout", type=int, default=180)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    output_dir = (
        pathlib.Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else pathlib.Path(tempfile.mkdtemp(prefix="unialloc-redox-current-source-"))
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    target_dir = pathlib.Path(tempfile.mkdtemp(prefix="cargo-target-", dir=str(output_dir)))
    cargo = shutil.which("cargo") or "cargo"
    rustup = shutil.which("rustup") or "rustup"
    base = [cargo, f"+{args.toolchain}"]
    target_args = ["--target", args.target, "--target-dir", str(target_dir)]
    features = ["--no-default-features", "--features", args.features]

    target_record, installed_text = run_command(
        "rustup-target-list",
        [rustup, "target", "list", "--installed", "--toolchain", args.toolchain],
        output_dir,
        args.timeout,
    )
    target_installed = args.target in installed_text.splitlines()
    checks: Dict[str, Any] = {"target_list": target_record}
    if target_installed:
        checks["library"] = run_command(
            "cargo-check-redox-lib",
            [*base, "check", "-p", "unialloc", "--lib", *target_args, *features],
            output_dir,
            args.timeout,
        )[0]
        checks["small_heap"] = run_command(
            "cargo-check-redox-small-heap",
            [
                *base,
                "check",
                "-p",
                "unialloc",
                "--example",
                "small_heap",
                *target_args,
                *features,
            ],
            output_dir,
            args.timeout,
        )[0]
        checks["codegen"] = run_command(
            "cargo-codegen-redox-object",
            [*base, "rustc", "-p", "unialloc", "--lib", *target_args, *features, "--", "--emit=obj"],
            output_dir,
            args.timeout,
        )[0]

    candidates = sorted((target_dir / args.target / "debug" / "deps").glob("unialloc-*.o"))
    object_path = output_dir / f"unialloc-{args.target}.o"
    if candidates:
        shutil.copy2(candidates[-1], object_path)
    object_check = elf_x86_64_relocatable_contract(object_path)

    nm = find_llvm_nm(args.toolchain) if object_check["passed"] else None
    nm_record: Optional[Dict[str, Any]] = None
    abi_check = symbol_contract(())
    if nm is not None:
        nm_record, nm_text = run_command(
            "llvm-nm-redox-object",
            [str(nm), "--defined-only", str(object_path)],
            output_dir,
            args.timeout,
        )
        if nm_record["passed"]:
            abi_check = symbol_contract(symbols_from_nm(nm_text))

    blockers = []
    if not target_installed:
        blockers.append(f"Rust target is not installed: {args.target}")
    blockers.extend(name + " failed" for name, record in checks.items() if not record["passed"])
    if not object_check["passed"]:
        blockers.append("Redox x86_64 ELF object was not produced")
    if nm is None:
        blockers.append("toolchain llvm-nm is unavailable")
    elif nm_record is not None and not nm_record["passed"]:
        blockers.append("Redox object symbol inspection failed")
    if not abi_check["passed"]:
        blockers.append("required constrained-platform C ABI symbols are missing")

    summary = {
        "schema_version": 1,
        "source": "redox-current-source-contract",
        "passed": not blockers,
        "blockers": blockers,
        "source_identity": git_identity(),
        "toolchain": args.toolchain,
        "target": args.target,
        "features": args.features.split(","),
        "target_installed": target_installed,
        "checks": checks,
        "object_contract": object_check,
        "llvm_nm": str(nm) if nm else None,
        "nm_check": nm_record,
        "c_abi_contract": abi_check,
        "validated_scope": "Redox target type-check, ELF codegen, and constrained-platform C ABI exports",
        "external_validation_missing": "final Redox link and target run require Redox linker/redoxer/QEMU",
        "claim_grade": False,
        "performance_evidence": False,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    shutil.rmtree(target_dir, ignore_errors=True)
    print(json.dumps({"summary": str(output_dir / "summary.json"), **summary}, indent=2, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
