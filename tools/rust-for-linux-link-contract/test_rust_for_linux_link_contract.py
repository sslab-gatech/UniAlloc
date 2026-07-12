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
import textwrap


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

C_ABI_LAYOUT_ASSERTIONS = r"""
#include "unialloc_kernel_ffi.h"

#define ASSERT_OFFSET(type, field, expected) \
    _Static_assert(offsetof(type, field) == (expected), #type "." #field " offset")

_Static_assert(UNIALLOC_SEMANTIC_STATS_SNAPSHOT_ABI_VERSION == 3u, "stats ABI version");
_Static_assert(UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION == 2u, "type stats ABI version");
_Static_assert(UNIALLOC_SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION == 1u, "fallback ABI version");
_Static_assert(UNIALLOC_SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION == 1u, "metadata ABI version");
_Static_assert(UNIALLOC_CONSTRAINED_BOOT_SAMPLE_ABI_VERSION == 2u, "boot sample ABI version");
_Static_assert(sizeof(void *) == 8, "contract requires a 64-bit C data model");
_Static_assert(sizeof(size_t) == 8, "contract requires 64-bit size_t");

_Static_assert(sizeof(UniallocSemanticStatsSnapshot) == 192, "stats size");
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, total_allocations, 0);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, typed_allocations, 8);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, fallback_allocations, 16);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, total_allocated_bytes, 24);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, typed_allocated_bytes, 32);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, fallback_allocated_bytes, 40);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, typed_deallocations, 48);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, fallback_deallocations, 56);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, policy_flags_seen, 64);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, last_type_id, 72);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, coverage_basis_points, 80);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, typed_cache_hits, 88);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, typed_cache_inserts, 96);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, typed_cache_bypasses, 104);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, delayed_free_enqueues, 112);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, delayed_free_flushes, 120);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, metadata_pac_auth_signs, 128);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, metadata_pac_auth_verifications, 136);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, metadata_pac_auth_failures, 144);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, metadata_pac_software_fallback_signs, 152);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, metadata_pac_software_fallback_verifications, 160);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, metadata_pac_software_fallback_failures, 168);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, total_deallocations, 176);
ASSERT_OFFSET(UniallocSemanticStatsSnapshot, semantic_type_stats_dropped_events, 184);

_Static_assert(sizeof(UniallocSemanticTypeStatsSnapshot) == 112, "type stats size");
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, type_id, 0);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, module_id, 8);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, callsite, 16);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, allocations, 24);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, allocated_bytes, 32);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, deallocations, 40);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, cache_hits, 48);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, cache_inserts, 56);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, cache_bypasses, 64);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, observed_alloc_size, 72);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, observed_alloc_align, 80);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, observed_dealloc_size, 88);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, observed_dealloc_align, 96);
ASSERT_OFFSET(UniallocSemanticTypeStatsSnapshot, policy_flags_seen, 104);

_Static_assert(sizeof(UniallocSemanticFallbackAttributionSnapshot) == 64, "fallback size");
ASSERT_OFFSET(UniallocSemanticFallbackAttributionSnapshot, raw_alloc_no_metadata, 0);
ASSERT_OFFSET(UniallocSemanticFallbackAttributionSnapshot, raw_alloc_no_metadata_bytes, 8);
ASSERT_OFFSET(UniallocSemanticFallbackAttributionSnapshot, raw_dealloc_no_metadata, 16);
ASSERT_OFFSET(UniallocSemanticFallbackAttributionSnapshot, raw_realloc_no_metadata, 24);
ASSERT_OFFSET(UniallocSemanticFallbackAttributionSnapshot, raw_realloc_no_metadata_bytes, 32);
ASSERT_OFFSET(UniallocSemanticFallbackAttributionSnapshot, raw_realloc_moved_dealloc_no_metadata, 40);
ASSERT_OFFSET(UniallocSemanticFallbackAttributionSnapshot, realloc_recorded_old_metadata_new_allocations, 48);
ASSERT_OFFSET(UniallocSemanticFallbackAttributionSnapshot, realloc_recorded_old_metadata_new_allocation_bytes, 56);

_Static_assert(sizeof(UniallocSemanticMetadataValidationSnapshot) == 64, "metadata size");
ASSERT_OFFSET(UniallocSemanticMetadataValidationSnapshot, recovery_identity_matches, 0);
ASSERT_OFFSET(UniallocSemanticMetadataValidationSnapshot, recovery_identity_mismatches, 8);
ASSERT_OFFSET(UniallocSemanticMetadataValidationSnapshot, last_mismatch_requested_type_id, 16);
ASSERT_OFFSET(UniallocSemanticMetadataValidationSnapshot, last_mismatch_recorded_type_id, 24);
ASSERT_OFFSET(UniallocSemanticMetadataValidationSnapshot, last_mismatch_requested_module_id, 32);
ASSERT_OFFSET(UniallocSemanticMetadataValidationSnapshot, last_mismatch_recorded_module_id, 40);
ASSERT_OFFSET(UniallocSemanticMetadataValidationSnapshot, last_mismatch_requested_callsite, 48);
ASSERT_OFFSET(UniallocSemanticMetadataValidationSnapshot, last_mismatch_recorded_callsite, 56);

_Static_assert(sizeof(UniallocConstrainedBootSample) == 152, "boot sample size");
ASSERT_OFFSET(UniallocConstrainedBootSample, boot_cycle, 0);
ASSERT_OFFSET(UniallocConstrainedBootSample, fixed_heap_ready, 8);
ASSERT_OFFSET(UniallocConstrainedBootSample, c_abi_invoked, 9);
ASSERT_OFFSET(UniallocConstrainedBootSample, c_abi_ready_after_init, 10);
ASSERT_OFFSET(UniallocConstrainedBootSample, c_abi_round_trips, 16);
ASSERT_OFFSET(UniallocConstrainedBootSample, c_abi_invalid_layouts_rejected, 24);
ASSERT_OFFSET(UniallocConstrainedBootSample, c_abi_over_page_alignment_checked, 32);
ASSERT_OFFSET(UniallocConstrainedBootSample, c_abi_over_page_alignment, 40);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_total_allocations, 48);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_total_deallocations, 56);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_typed_allocations, 64);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_typed_deallocations, 72);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_fallback_allocations, 80);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_fallback_deallocations, 88);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_total_allocated_bytes, 96);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_typed_allocated_bytes, 104);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_fallback_allocated_bytes, 112);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_coverage_basis_points, 120);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_type_stats_rows, 128);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_type_stats_dropped_events, 136);
ASSERT_OFFSET(UniallocConstrainedBootSample, allocator_type_stats_probe_matched, 144);
"""


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
    cc = shutil.which("cc")
    if cargo is None or nm is None or cc is None:
        raise RuntimeError("cargo, nm, and a C11 cc are required for the link regression")

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

        c_source = Path(target_dir) / "unialloc_kernel_ffi_layout.c"
        c_object = Path(target_dir) / "unialloc_kernel_ffi_layout.o"
        c_source.write_text(textwrap.dedent(C_ABI_LAYOUT_ASSERTIONS), encoding="utf-8")
        run(
            [
                cc,
                "-std=c11",
                "-Werror",
                "-c",
                str(c_source),
                "-I",
                str(ROOT / "kernel" / "kernel-modules" / "benchmarking"),
                "-o",
                str(c_object),
            ]
        )
        c_compiler_target = run([cc, "-dumpmachine"]).stdout.strip()
        c_compiler_version = run([cc, "--version"]).stdout.splitlines()[0]

        summary = {
            "source": "unialloc-rust-for-linux-final-crate-link-contract",
            "target": TARGET,
            "toolchain": toolchain,
            "build_succeeded": True,
            "linked_elf": True,
            "rust_bench_force_link_import": True,
            "rust_runtime_bridge_abi_layout_compile_time_checked": True,
            "c_header_local_host_64_bit_layout_compile_time_checked": True,
            "c_header_all_field_offsets_checked": True,
            "c_compiler_target": c_compiler_target,
            "c_compiler_version": c_compiler_version,
            "abi_layout_records_checked": 5,
            "abi_layout_field_offsets_checked_per_language": 75,
            "required_symbols": sorted(REQUIRED_SYMBOLS),
            "binary_sha256": hashlib.sha256(image).hexdigest(),
            "validated": True,
            "kernel_module_runtime_validation": False,
            "external_assets_required": ["Rust-for-Linux kernel tree", "kernel runner"],
            "claim_boundary": (
                "Rust layout is checked in the x86_64-unknown-none no_std final crate; "
                "the C header is checked with the local host 64-bit C11 compiler, not "
                "the Rust-for-Linux kernel compiler. This does not prove kernel-module "
                "loading, runtime behavior, or performance."
            ),
            "build_stderr_tail": build.stderr.splitlines()[-8:],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
