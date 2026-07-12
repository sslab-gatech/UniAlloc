#!/usr/bin/env python3
"""Build and inspect the Rust-for-Linux UniAlloc final-crate link contract."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tools" / "rust-for-linux-link-contract" / "Cargo.toml"
RUST_BENCH = ROOT / "kernel" / "kernel-modules" / "benchmarking" / "rust_bench.rs"
TARGET = "x86_64-unknown-none"
BINARY_NAME = "unialloc_rust_for_linux_link_contract"
FORCE_LINK_IMPORT = "extern crate unialloc as _;"
REQUIRED_SYMBOLS = {
    "_start",
    "rust_for_linux_unialloc_link_probe",
    "unialloc_alloc",
    "unialloc_dealloc",
    "unialloc_fixed_heap_try_init",
    "unialloc_fixed_heap_try_extend",
    "unialloc_fixed_heap_ready",
    "unialloc_realloc",
    "__unialloc_semantic_stats_snapshot_checked",
    "__unialloc_semantic_type_stats_snapshot_checked",
    "__unialloc_semantic_metadata_validation_snapshot_checked",
}


def run(
    command: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rust_bench_source = RUST_BENCH.read_text(encoding="utf-8")
    if FORCE_LINK_IMPORT not in rust_bench_source:
        raise RuntimeError(
            "Rust-for-Linux final crate does not explicitly force-link UniAlloc"
        )

    cargo = shutil.which("cargo")
    nm = shutil.which("nm")
    if cargo is None or nm is None:
        raise RuntimeError("cargo and nm are required for the link regression")

    with tempfile.TemporaryDirectory(prefix="unialloc-rfl-link-contract-") as target_dir:
        env = os.environ.copy()
        env["CARGO_TARGET_DIR"] = target_dir
        command = [
            cargo,
            f"+{toolchain}",
            "build",
            "--offline",
            "--locked",
            "-Z",
            "build-std=core,alloc,compiler_builtins",
            "--manifest-path",
            str(MANIFEST),
            "--target",
            TARGET,
        ]
        build = run(command, env=env)
        binary = Path(target_dir) / TARGET / "debug" / BINARY_NAME
        if not binary.is_file():
            raise RuntimeError(f"expected linked Rust-for-Linux contract ELF: {binary}")

        image = binary.read_bytes()
        if image[:4] != b"\x7fELF":
            raise RuntimeError("Rust-for-Linux link contract output is not an ELF image")

        symbol_output = run([nm, str(binary)]).stdout
        present_symbols = {
            line.rsplit(maxsplit=1)[-1]
            for line in symbol_output.splitlines()
            if line.split()
        }
        missing_symbols = sorted(REQUIRED_SYMBOLS - present_symbols)
        if missing_symbols:
            raise RuntimeError(f"linked UniAlloc symbols missing: {missing_symbols}")

        summary = {
            "source": "unialloc-rust-for-linux-final-crate-link-contract",
            "target": TARGET,
            "toolchain": toolchain,
            "build_succeeded": True,
            "linked_elf": True,
            "rust_bench_force_link_import": True,
            "required_symbols": sorted(REQUIRED_SYMBOLS),
            "binary_sha256": hashlib.sha256(image).hexdigest(),
            "validated": True,
            "kernel_module_runtime_validation": False,
            "external_assets_required": ["Rust-for-Linux kernel tree", "kernel runner"],
            "claim_boundary": (
                "Local no_std final-crate build/link/symbol evidence only; this does not "
                "prove kernel-module loading, runtime behavior, or performance."
            ),
            "build_stderr_tail": build.stderr.splitlines()[-8:],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
