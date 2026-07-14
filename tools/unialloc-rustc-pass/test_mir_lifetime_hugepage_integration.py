#!/usr/bin/env python3
"""End-to-end gate from an exact MIR lifetime profile to a HugeTLB payload."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
TOOLCHAIN = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
HUGETLB_EXTENT_BYTES = 2 * 1024 * 1024


def hugepage_pool() -> dict[str, int]:
    """Return the configured 2 MiB HugeTLB pool counters from procfs."""

    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return {}
    counters: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8").splitlines():
        fields = line.replace(":", "").split()
        if len(fields) >= 2 and fields[0] in {
            "HugePages_Total",
            "HugePages_Free",
            "HugePages_Rsvd",
            "HugePages_Surp",
            "Hugepagesize",
        }:
            counters[fields[0]] = int(fields[1])
    return counters


class MirLifetimeHugepageIntegrationTest(unittest.TestCase):
    """Exercise the real rustc rewrite, allocator ABI, and HugeTLB mapping."""

    @classmethod
    def setUpClass(cls) -> None:
        if not sys.platform.startswith("linux"):
            raise unittest.SkipTest("the HugeTLB integration gate requires Linux procfs")
        pool = hugepage_pool()
        if pool.get("HugePages_Total", 0) == 0:
            raise unittest.SkipTest("no persistent HugeTLB pool is configured on this host")
        if pool.get("Hugepagesize") != 2048:
            raise unittest.SkipTest("the allocator gate currently targets 2 MiB HugeTLB pages")
        unreserved_free = pool.get("HugePages_Free", 0) - pool.get("HugePages_Rsvd", 0)
        if unreserved_free <= 0:
            raise AssertionError(
                "the configured HugeTLB pool has no unreserved free page for the gate"
            )

        cls._tmp = tempfile.TemporaryDirectory(
            prefix="unialloc-lifetime-hugepage-integration-",
            dir="/tmp" if Path("/tmp").is_dir() else None,
        )
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.tmp = Path(cls._tmp.name)
        cls.rustc = shutil.which("rustc") or "rustc"
        cls.cargo = shutil.which("cargo") or "cargo"
        cls.sysroot = Path(
            subprocess.check_output(
                [cls.rustc, f"+{TOOLCHAIN}", "--print", "sysroot"],
                cwd=ROOT,
                text=True,
            ).strip()
        )

        cls.driver = cls.tmp / "unialloc-rustc-mir-rewrite-dry-run"
        driver_env = os.environ.copy()
        driver_env["RUSTC_BOOTSTRAP"] = "1"
        cls.run_checked(
            [
                cls.rustc,
                f"+{TOOLCHAIN}",
                "--cfg",
                "unialloc_rustc_current",
                str(PASS_SOURCE),
                "-o",
                str(cls.driver),
            ],
            env=driver_env,
        )

        # Build an isolated feature-specific rlib. The repository's default
        # abort profile is also applied to the direct rustc fixture below.
        cls.cargo_target = cls.tmp / "cargo-target"
        cargo_env = os.environ.copy()
        cargo_env["CARGO_TARGET_DIR"] = str(cls.cargo_target)
        for name in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
            cargo_env.pop(name, None)
        cls.run_checked(
            [
                cls.cargo,
                "build",
                "-p",
                "unialloc",
                "--lib",
                "--features",
                "lifetime_hugepage",
            ],
            env=cargo_env,
        )
        cls.unialloc_rlib = cls.cargo_target / "debug/libunialloc.rlib"
        cls.dependency_dir = cls.cargo_target / "debug/deps"
        if not cls.unialloc_rlib.is_file():
            raise AssertionError(f"cargo did not produce {cls.unialloc_rlib}")

        cls.fixture = cls.tmp / "lifetime_hugepage_profile_probe.rs"
        cls.fixture.write_text(
            r'''extern crate unialloc;

use unialloc::{
    lifetime_hugepage_configure, lifetime_hugepage_stats_reset,
    lifetime_hugepage_stats_snapshot, LifetimeHugepagePolicy, UniAlloc,
};

#[global_allocator]
static GLOBAL: UniAlloc = UniAlloc;

#[inline(never)]
fn long_lived_site() -> Vec<u64> {
    Vec::with_capacity(16)
}

fn main() {
    assert!(lifetime_hugepage_stats_reset());
    assert!(lifetime_hugepage_configure(
        LifetimeHugepagePolicy::LongLivedHugepage,
    ));

    let mut value = long_lived_site();
    assert_eq!(value.len(), 0);
    let active = lifetime_hugepage_stats_snapshot();
    assert_eq!(active.extent_bytes, 2 * 1024 * 1024, "{active:?}");
    assert_eq!(active.routed_allocations, 1, "{active:?}");
    assert_eq!(active.hugetlb_extent_mappings, 1, "{active:?}");
    assert_eq!(active.hugetlb_fallback_extent_mappings, 0, "{active:?}");
    assert_eq!(active.mapping_failures, 0, "{active:?}");
    assert_eq!(active.current_hugetlb_extents, 1, "{active:?}");
    assert_eq!(active.live_objects, 1, "{active:?}");
    assert_eq!(active.live_long_lived_objects, 1, "{active:?}");
    assert_eq!(active.retained_bytes, active.extent_bytes, "{active:?}");

    // The same exact Vec identity and long-lived profile row wraps this
    // deallocation-capable receiver call. An empty Vec shrinks to capacity 0,
    // so the arena must release its sole 2 MiB payload mapping here.
    value.shrink_to_fit();
    assert_eq!(value.capacity(), 0);
    let released = lifetime_hugepage_stats_snapshot();
    assert_eq!(released.routed_allocations, 1, "{released:?}");
    assert_eq!(released.routed_deallocations, 1, "{released:?}");
    assert_eq!(released.extent_unmaps, 1, "{released:?}");
    assert_eq!(released.extent_unmap_failures, 0, "{released:?}");
    assert_eq!(released.current_extents, 0, "{released:?}");
    assert_eq!(released.current_hugetlb_extents, 0, "{released:?}");
    assert_eq!(released.live_objects, 0, "{released:?}");
    assert!(released.all_mappings_released, "{released:?}");

    assert!(lifetime_hugepage_configure(LifetimeHugepagePolicy::Disabled));
    println!(
        "{{\"extent_bytes\":{},\"hugetlb_mappings\":{},\"hugetlb_fallbacks\":{},\"extent_unmaps\":{},\"routed_allocations\":{},\"routed_deallocations\":{},\"released\":{}}}",
        active.extent_bytes,
        active.hugetlb_extent_mappings,
        active.hugetlb_fallback_extent_mappings,
        released.extent_unmaps,
        released.routed_allocations,
        released.routed_deallocations,
        released.all_mappings_released,
    );
}
''',
            encoding="utf-8",
        )

    @classmethod
    def run_checked(
        cls, command: list[str], *, env: dict[str, str] | None = None
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
            raise AssertionError(
                f"command failed ({completed.returncode}): {' '.join(command)}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return completed

    def run_pass(
        self, label: str, *, profile: Path | None = None, actual_rewrite: bool = False
    ) -> dict[str, object]:
        audit = self.tmp / f"{label}.json"
        command = [
            str(self.driver),
            "--unialloc-rewrite-audit-out",
            str(audit),
            "--unialloc-continue-compilation",
        ]
        if actual_rewrite:
            command.append("--unialloc-actual-semantic-scope-rewrite")
        if profile is not None:
            command.extend(["--unialloc-lifetime-profile", str(profile)])
        command.extend(
            [
                "--",
                "--sysroot",
                str(self.sysroot),
                "--edition=2021",
                "-C",
                "panic=abort",
                "-L",
                f"dependency={self.dependency_dir}",
                "--extern",
                f"unialloc={self.unialloc_rlib}",
                "-o",
                str(self.tmp / label),
                str(self.fixture),
            ]
        )
        env = os.environ.copy()
        library_path = str(self.sysroot / "lib")
        env["LD_LIBRARY_PATH"] = library_path
        env["DYLD_LIBRARY_PATH"] = library_path
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return json.loads(audit.read_text(encoding="utf-8"))

    def target_row(
        self,
        audit: dict[str, object],
        *,
        function: str,
        lowering_kind: str,
        callee_fragment: str | None = None,
    ) -> dict[str, object]:
        rows = [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(function)
            and row.get("lowering_kind") == lowering_kind
            and (
                callee_fragment is None
                or callee_fragment in str(row.get("callee") or "")
            )
            and row.get("semantic_object_type")
            == "std::vec::Vec<u64, std::alloc::Global>"
        ]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    @staticmethod
    def profile_line(row: dict[str, object]) -> str:
        return f"{row['callsite']} {row['type_id']} {row['module_id']} long-lived"

    def wait_for_pool_restore(self, free_before: int) -> dict[str, int]:
        deadline = time.monotonic() + 2.0
        pool = hugepage_pool()
        while pool.get("HugePages_Free", 0) < free_before and time.monotonic() < deadline:
            time.sleep(0.02)
            pool = hugepage_pool()
        return pool

    def test_exact_profile_drives_real_hugetlb_payload_and_release(self) -> None:
        baseline = self.run_pass("baseline")
        allocation = self.target_row(
            baseline,
            function="long_lived_site",
            lowering_kind="semantic_scope_enter_exit_rewrite",
            callee_fragment="with_capacity",
        )
        shrink = self.target_row(
            baseline,
            function="main",
            lowering_kind="semantic_scope_enter_exit_rewrite",
            callee_fragment="shrink_to_fit",
        )
        final_drop = self.target_row(
            baseline,
            function="main",
            lowering_kind="semantic_scope_drop_rewrite",
        )
        profiled_rows = [allocation, shrink, final_drop]
        self.assertTrue(all(int(row["type_id"]) != 0 for row in profiled_rows))
        self.assertTrue(all(int(row["module_id"]) != 0 for row in profiled_rows))
        self.assertEqual({row["type_id"] for row in profiled_rows}, {allocation["type_id"]})
        self.assertEqual(
            {row["module_id"] for row in profiled_rows}, {allocation["module_id"]}
        )

        profile = self.tmp / "exact-long-lived.profile"
        profile.write_text(
            "\n".join(
                ["unialloc-lifetime-profile-v1"]
                + [self.profile_line(row) for row in profiled_rows]
                + [""]
            ),
            encoding="utf-8",
        )
        actual = self.run_pass("actual", profile=profile, actual_rewrite=True)

        rows_by_callsite = {
            row.get("callsite"): row
            for row in actual.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
        }
        expected_statuses = {
            allocation["callsite"]: "actual_semantic_scope_enter_exit_rewrite_applied",
            shrink["callsite"]: "actual_semantic_scope_enter_exit_rewrite_applied",
            final_drop["callsite"]: "actual_semantic_scope_drop_rewrite_applied",
        }
        for callsite, expected_status in expected_statuses.items():
            row = rows_by_callsite.get(callsite)
            self.assertIsNotNone(row, callsite)
            assert row is not None
            self.assertEqual(row["lifetime_hint"], 2, row)
            self.assertEqual(row["lifetime_hint_basis"], "profile_exact_match", row)
            self.assertEqual(row["rewrite_status"], expected_status, row)

        compiler_pass = actual["compiler_pass"]
        self.assertTrue(compiler_pass["lifetime_profile_format_valid"])
        self.assertEqual(
            compiler_pass["lifetime_profile_binding"],
            "exact(callsite,type_id,module_id)",
        )
        self.assertEqual(compiler_pass["lifetime_profile_match_count"], 3)
        self.assertEqual(compiler_pass["lifetime_profile_miss_count"], 0)
        self.assertRegex(str(compiler_pass["lifetime_profile_digest"]), r"^[0-9a-f]{16}$")

        pool_before = hugepage_pool()
        free_before = pool_before.get("HugePages_Free", 0)
        self.assertGreater(
            free_before - pool_before.get("HugePages_Rsvd", 0), 0, pool_before
        )
        completed = subprocess.run(
            [str(self.tmp / "actual")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        pool_after = self.wait_for_pool_restore(free_before)
        self.assertEqual(
            completed.returncode,
            0,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertEqual(pool_after.get("HugePages_Total"), pool_before.get("HugePages_Total"))
        self.assertGreaterEqual(pool_after.get("HugePages_Free", 0), free_before, pool_after)

        runtime = json.loads(completed.stdout.strip())
        self.assertEqual(runtime["extent_bytes"], HUGETLB_EXTENT_BYTES)
        self.assertEqual(runtime["hugetlb_mappings"], 1)
        self.assertEqual(runtime["hugetlb_fallbacks"], 0)
        self.assertEqual(runtime["extent_unmaps"], 1)
        self.assertEqual(runtime["routed_allocations"], 1)
        self.assertEqual(runtime["routed_deallocations"], 1)
        self.assertTrue(runtime["released"])


if __name__ == "__main__":
    unittest.main()
