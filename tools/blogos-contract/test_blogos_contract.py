#!/usr/bin/env python3
"""Build and inspect the dependency-free BlogOS/UniAlloc kernel contract."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tools" / "blogos-contract" / "Cargo.toml"
TARGET = "x86_64-unknown-none"
REQUIRED_SYMBOLS = {
    "_start",
    "blogos_unialloc_boot_init",
    "blogos_unialloc_contract_probe",
}
REQUIRED_SYMBOL_FRAGMENTS = {
    "GLOBAL_ALLOCATOR",
    "BlogOsGlobalAllocator",
    "___rust_alloc",
    "___rust_alloc_error_handler",
    "___rust_dealloc",
    "rust_begin_unwind",
}


def run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
    cargo = shutil.which("cargo")
    nm = shutil.which("nm")
    if cargo is None or nm is None:
        raise RuntimeError("cargo and nm are required for the BlogOS contract regression")

    with tempfile.TemporaryDirectory(prefix="unialloc-blogos-contract-") as target_dir:
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
        binary = Path(target_dir) / TARGET / "debug" / "unialloc_blogos_contract"
        if not binary.is_file():
            raise RuntimeError(f"expected linked BlogOS contract binary: {binary}")
        image = binary.read_bytes()
        if image[:4] != b"\x7fELF":
            raise RuntimeError("BlogOS contract output is not an ELF target image")

        symbol_output = run([nm, str(binary)]).stdout
        present_symbols = {
            line.rsplit(maxsplit=1)[-1]
            for line in symbol_output.splitlines()
            if line.split()
        }
        missing_symbols = sorted(REQUIRED_SYMBOLS - present_symbols)
        if missing_symbols:
            raise RuntimeError(f"BlogOS contract symbols missing: {missing_symbols}")
        missing_fragments = sorted(
            fragment
            for fragment in REQUIRED_SYMBOL_FRAGMENTS
            if fragment not in symbol_output
        )
        if missing_fragments:
            raise RuntimeError(
                "BlogOS global allocator or handler symbols missing: "
                f"{missing_fragments}"
            )

        summary = {
            "source": "unialloc-blogos-build-contract",
            "target": TARGET,
            "toolchain": toolchain,
            "build_succeeded": True,
            "linked_elf": True,
            "required_symbols": sorted(REQUIRED_SYMBOLS),
            "required_symbol_fragments": sorted(REQUIRED_SYMBOL_FRAGMENTS),
            "binary_sha256": hashlib.sha256(image).hexdigest(),
            "global_allocator_contract": True,
            "boot_heap_init_contract": True,
            "panic_and_alloc_error_handlers": True,
            "validated": True,
            "runtime_boot_validation": False,
            "external_assets_required": ["BlogOS bootloader/image", "QEMU or hardware runner"],
            "claim_boundary": (
                "Build/link/ABI evidence only; this does not prove a BlogOS boot, "
                "runtime allocator behavior, or performance."
            ),
            "build_stderr_tail": build.stderr.splitlines()[-8:],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
