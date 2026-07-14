#!/usr/bin/env python3
"""Regression checks for the fail-closed Scudo benchmark runtime guard."""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BENCH_DIR = REPO_ROOT / "unialloc" / "benches"
DRIVER_PATH = REPO_ROOT / "evaluation" / "scripts" / "paper_workload_driver.py"

spec = importlib.util.spec_from_file_location("scudo_guard_paper_driver", DRIVER_PATH)
assert spec is not None and spec.loader is not None
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class ScudoBenchmarkRuntimeGuardTests(unittest.TestCase):
    def test_every_scudo_benchmark_root_installs_the_runtime_guard(self) -> None:
        for relative_path in ("lib.rs", "vec_deque_append.rs", "multi1.rs", "multi2.rs"):
            source = (BENCH_DIR / relative_path).read_text(encoding="utf-8")
            self.assertIn('#[cfg(feature = "bench_scudo")]', source)
            self.assertIn("mod scudo_runtime;", source)

    def test_unialloc_only_registered_benches_reject_scudo_labels(self) -> None:
        for relative_path in (
            "semantic_std.rs",
            "std_bench_compiler_proto_generated.rs",
            "std_bench_compiler_exact_dynamic_generated.rs",
        ):
            source = (BENCH_DIR / relative_path).read_text(encoding="utf-8")
            self.assertIn('#[cfg(feature = "bench_scudo")]', source)
            self.assertIn("compile_error!", source)

        std_bench = (BENCH_DIR / "lib.rs").read_text(encoding="utf-8")
        self.assertIn(
            '#![cfg(any(not(target_os = "android"), feature = "bench_scudo"))]',
            std_bench,
        )

    def test_guard_checks_scudo_identity_and_allocator_symbol_providers(self) -> None:
        source = (BENCH_DIR / "scudo_runtime.rs").read_text(encoding="utf-8")
        for symbol in (
            "__scudo_print_stats",
            "malloc",
            "calloc",
            "realloc",
            "free",
            "posix_memalign",
        ):
            self.assertIn(symbol, source)
        self.assertIn("libc::dlsym", source)
        self.assertIn("libc::dladdr", source)
        self.assertIn("dli_fbase", source)
        self.assertIn("UNIALLOC_SCUDO_RUNTIME_LIBRARY", source)
        self.assertIn("libc::realpath", source)
        self.assertIn("provider_matches_configured_runtime", source)
        self.assertIn('link_section = ".init_array"', source)
        self.assertNotIn('unsafe(link_section = ".init_array")', source)

    def test_guard_has_stable_attestation_and_fail_closed_markers(self) -> None:
        source = (BENCH_DIR / "scudo_runtime.rs").read_text(encoding="utf-8")
        self.assertIn("unialloc: verified Scudo runtime identity", source)
        self.assertIn(
            "unialloc: bench_scudo requires a verified Scudo runtime", source
        )
        self.assertIn("libc::_exit(EXIT_SCUDO_UNVERIFIED)", source)

    def test_scudo_cannot_be_combined_with_another_allocator_label(self) -> None:
        source = (BENCH_DIR / "scudo_runtime.rs").read_text(encoding="utf-8")
        for feature in (
            "bench_ourself",
            "bench_ptmalloc",
            "bench_jemalloc",
            "bench_mimalloc",
            "bench_tcmalloc",
            "bench_snmalloc",
        ):
            self.assertIn(f'feature = "{feature}"', source)
        self.assertIn(
            "bench_scudo must be the only enabled bench allocator feature", source
        )

    def test_authenticated_preload_rejects_executable_owned_allocator_symbols(self) -> None:
        if os.name != "posix" or shutil.which("cc") is None or shutil.which("cargo") is None:
            self.skipTest("provider-preemption regression requires Linux cc and cargo")
        probe_args = argparse.Namespace(
            scudo_mode="ld-preload",
            scudo_runtime_library=None,
            rust_toolchain=None,
        )
        runtime_probe = driver.scudo_runtime_probe(probe_args)
        if runtime_probe.get("ok") is not True:
            self.skipTest("no authenticated standalone Scudo runtime is installed")
        runtime = str(runtime_probe["runtime_library_identity"]["realpath"])
        toolchain = (REPO_ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()

        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            c_source = tmp / "preempt.c"
            c_object = tmp / "preempt.o"
            c_source.write_text(
                r'''
#include <stddef.h>
#include <errno.h>
extern void *__libc_malloc(size_t);
extern void *__libc_calloc(size_t, size_t);
extern void *__libc_realloc(void *, size_t);
extern void __libc_free(void *);
extern void *__libc_memalign(size_t, size_t);
void __scudo_print_stats(void) {}
void *malloc(size_t n) { return __libc_malloc(n); }
void *calloc(size_t n, size_t s) { return __libc_calloc(n, s); }
void *realloc(void *p, size_t n) { return __libc_realloc(p, n); }
void free(void *p) { __libc_free(p); }
int posix_memalign(void **out, size_t a, size_t n) {
    if (!out || a < sizeof(void *) || (a & (a - 1))) return EINVAL;
    void *p = __libc_memalign(a, n);
    if (!p) return ENOMEM;
    *out = p;
    return 0;
}
'''.lstrip(),
                encoding="utf-8",
            )
            compile_c = subprocess.run(
                ["cc", "-c", "-fPIC", str(c_source), "-o", str(c_object)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            self.assertEqual(compile_c.returncode, 0, compile_c.stderr)

            crate = tmp / "probe"
            (crate / "src").mkdir(parents=True)
            feature_names = (
                "bench_scudo",
                "bench_ourself",
                "bench_ptmalloc",
                "bench_jemalloc",
                "bench_mimalloc",
                "bench_tcmalloc",
                "bench_snmalloc",
            )
            feature_lines = "\n".join(f"{name} = []" for name in feature_names)
            (crate / "Cargo.toml").write_text(
                "\n".join(
                    [
                        "[package]",
                        'name = "scudo-provider-preemption-probe"',
                        'version = "0.0.0"',
                        'edition = "2021"',
                        "[features]",
                        feature_lines,
                        "[dependencies]",
                        'libc = "0.2"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (crate / "src" / "main.rs").write_text(
                f'#[path = "{BENCH_DIR / "scudo_runtime.rs"}"]\n'
                "mod scudo_runtime;\nfn main() {}\n",
                encoding="utf-8",
            )
            build_env = os.environ.copy()
            build_env["RUSTFLAGS"] = (
                f"-C link-arg={c_object} -C link-arg=-Wl,--export-dynamic"
            )
            build = subprocess.run(
                ["cargo", f"+{toolchain}", "build", "--offline", "--features", "bench_scudo", "-q"],
                cwd=str(crate),
                env=build_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=180,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            executable = crate / "target" / "debug" / "scudo-provider-preemption-probe"
            run_env = os.environ.copy()
            run_env["LD_PRELOAD"] = runtime
            run_env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"] = runtime
            run = subprocess.run(
                [str(executable)],
                env=run_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            self.assertEqual(run.returncode, 86, run.stderr)
            self.assertIn("requires a verified Scudo runtime", run.stderr)
            self.assertNotIn("verified Scudo runtime identity", run.stderr)


if __name__ == "__main__":
    unittest.main()
