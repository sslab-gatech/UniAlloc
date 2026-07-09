#!/usr/bin/env python3
"""Regression tests for cargo-bench JSON wrapper raw evidence.

The test executes a tiny real Python child process that prints a libtest-style
bench row.  It does not run the paper benchmark matrix, but it validates the
same stdout/stderr retention and sha256 evidence path used by real external
cargo-bench workloads.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
WRAPPER_PATH = ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py"

spec = importlib.util.spec_from_file_location("paper_external_cargo_bench_json", WRAPPER_PATH)
assert spec is not None and spec.loader is not None
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


def write_fake_polars_checkout(root: pathlib.Path) -> pathlib.Path:
    checkout = root / "polars-checkout"
    benches = checkout / "benches"
    benches.mkdir(parents=True)
    legacy_benches = checkout / "polars" / "benches"
    legacy_benches.mkdir(parents=True)
    bench_entries = []
    for name in sorted(wrapper.RPOLARS_PAPER_CARGO_BENCH_TARGETS):
        (benches / f"{name}.rs").write_text("// fake bench source\n", encoding="utf-8")
        (legacy_benches / f"{name}.rs").write_text("// fake legacy-layout bench source\n", encoding="utf-8")
        bench_entries.append(f'[[bench]]\nname = "{name}"\npath = "benches/{name}.rs"\n')
    (checkout / "Cargo.toml").write_text(
        '[package]\nname = "polars"\nversion = "0.13.0"\nedition = "2021"\n\n'
        + "\n".join(bench_entries),
        encoding="utf-8",
    )
    return checkout


def write_fake_cargo(bin_dir: pathlib.Path) -> pathlib.Path:
    cargo = bin_dir / "cargo"
    cargo.write_text(
        "#!/usr/bin/env python3\n"
        "print('test bench_unit ... bench:       1,234 ns/iter (+/- 56)')\n",
        encoding="utf-8",
    )
    cargo.chmod(0o755)
    return cargo


def write_rpolars_csv(root: pathlib.Path, rows: int = 1) -> tuple[pathlib.Path, str]:
    csv_path = root / wrapper.RPOLARS_CANONICAL_GROUPBY_CSV_NAME
    with csv_path.open("w", encoding="utf-8") as handle:
        handle.write(",".join(wrapper.POLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + "\n")
        for index in range(rows):
            handle.write(f"a{index},b{index},c{index},d{index},e{index},f{index},1,2,3\n")
    return csv_path, hashlib.sha256(csv_path.read_bytes()).hexdigest()


def rpolars_input_contract(csv_path: pathlib.Path, digest: str, rows: int = 1) -> dict:
    return {
        "source": "rpolars-input-provenance-audit",
        "required_for_targets": True,
        "claim_grade_ready": True,
        "blockers": [],
        "expected_csv_name": wrapper.RPOLARS_CANONICAL_GROUPBY_CSV_NAME,
        "expected_source": wrapper.RPOLARS_CANONICAL_INPUT_SOURCE,
        "expected_datagen_args": {"n_rows": rows, "csv_name": wrapper.RPOLARS_CANONICAL_GROUPBY_CSV_NAME},
        "input_contract": {
            "claim_grade": True,
            "source": wrapper.RPOLARS_CANONICAL_INPUT_SOURCE,
            "expected_csv_name": wrapper.RPOLARS_CANONICAL_GROUPBY_CSV_NAME,
            "expected_rows": rows,
            "csv_sha256": digest,
        },
        "db_benchmark_source": {
            "source": wrapper.RPOLARS_CANONICAL_INPUT_SOURCE,
            "canonical_datagen_args": {"n_rows": rows, "csv_name": wrapper.RPOLARS_CANONICAL_GROUPBY_CSV_NAME},
        },
        "configured_csv": {
            "exists": True,
            "missing_columns": [],
            "resolved_path": str(csv_path),
            "configured_csv_src": str(csv_path),
            "basename": csv_path.name,
            "sha256": digest,
            "data_row_count": rows,
            "header": list(wrapper.POLARS_GROUPBY_REQUIRED_CSV_COLUMNS),
        },
    }


class CargoBenchJsonRawEvidenceTests(unittest.TestCase):
    def test_successful_child_emits_sha256_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = pathlib.Path(tmp) / "evidence"
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "unit-cargo-bench",
                "--allocator",
                "unialloc",
                "--run-index",
                "1",
                "--bench-name-filter",
                "bench_unit",
                "--evidence-dir",
                str(evidence_dir),
                "--max-output-bytes",
                "4096",
                "--",
                sys.executable,
                "-c",
                (
                    "import os, sys; "
                    "assert os.environ.get('CARGO_REGISTRIES_CRATES_IO_PROTOCOL') == 'sparse', os.environ.get('CARGO_REGISTRIES_CRATES_IO_PROTOCOL'); "
                    "assert os.environ.get('CARGO_NET_RETRY') == '2', os.environ.get('CARGO_NET_RETRY'); "
                    "assert os.environ.get('CARGO_NET_OFFLINE') == 'true', os.environ.get('CARGO_NET_OFFLINE'); "
                    "print('test bench_unit ... bench:       1,234 ns/iter (+/- 56)'); "
                    "print('child stderr retained', file=sys.stderr)"
                ),
            ]
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = wrapper.main(argv=argv)

            records = [
                json.loads(line)
                for line in stdout.getvalue().splitlines()
                if line.strip().startswith("{")
            ]
            self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
            self.assertTrue(records, stdout.getvalue())
            record = records[-1]
            self.assertTrue(record["success"], record)
            self.assertEqual(record["bench_row_count"], 1, record)
            self.assertEqual(len(record["evidence"]), 2, record)
            self.assertEqual(len(record["target_records"][0]["evidence"]), 2, record)
            for entry in record["evidence"]:
                path = pathlib.Path(entry["path"])
                self.assertTrue(path.exists(), entry)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])

    def test_cargo_bench_defaults_to_locked_offline(self) -> None:
        command = [
            "cargo",
            "+nightly-2022-07-01",
            "bench",
            "--features",
            "bench_ourself",
            "--bench",
            "deflate",
            "deflate_8_bits_strategy_0",
        ]
        routed, record = wrapper.inject_cargo_bench_reproducibility_flags(command, {})
        self.assertTrue(record["routed"], record)
        self.assertTrue(record["changed"], record)
        self.assertIn("--locked", routed)
        self.assertIn("--offline", routed)
        self.assertLess(routed.index("--offline"), routed.index("--bench"))

    def test_allocator_routed_cargo_bench_allows_offline_lock_refresh(self) -> None:
        command = ["cargo", "+nightly-2022-07-01", "bench", "--bench", "deflate"]
        routed, record = wrapper.inject_cargo_bench_reproducibility_flags(
            command,
            {},
            allow_lockfile_update=True,
        )
        self.assertTrue(record["routed"], record)
        self.assertTrue(record["allow_lockfile_update"], record)
        self.assertIn("--offline", routed)
        self.assertNotIn("--locked", routed)

    def test_cargo_bench_network_opt_out_preserves_command(self) -> None:
        command = ["cargo", "bench", "--bench", "deflate"]
        routed, record = wrapper.inject_cargo_bench_reproducibility_flags(
            command,
            {"UNIALLOC_PAPER_CARGO_ALLOW_NETWORK": "1"},
        )
        self.assertEqual(routed, command)
        self.assertFalse(record["changed"], record)
        self.assertTrue(record["network_allowed"], record)

    def test_strict_roxipng_fragment_contract_can_emit_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = pathlib.Path(tmp) / "evidence"
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            }
            paper_source = {
                "paper_benchmark": "R-Oxipng*",
                "paper_project": "Oxipng",
                "paper_version": "v4.0.3",
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "type_isolation",
                "--benchmark",
                "R-Oxipng*",
                "--allocator",
                "jemalloc",
                "--run-index",
                "1",
                "--cargo-bench-targets",
                "deflate",
                "--claim-grade-contract",
                "roxipng-paper-fragment",
                "--expected-cargo-bench-targets",
                "deflate,filters",
                "--expected-libtest-bench-functions",
                "bench_unit",
                "--paper-source-contract-json",
                json.dumps(source_contract),
                "--paper-source-json",
                json.dumps(paper_source),
                "--evidence-dir",
                str(evidence_dir),
                "--max-output-bytes",
                "4096",
                "--",
                sys.executable,
                "-c",
                "print('test bench_unit ... bench:       1,234 ns/iter (+/- 56)')",
            ]
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = wrapper.main(argv=argv)

            records = [
                json.loads(line)
                for line in stdout.getvalue().splitlines()
                if line.strip().startswith("{")
            ]
            self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
            record = records[-1]
            self.assertTrue(record["success"], record)
            self.assertTrue(record["claim_grade"], record)
            self.assertEqual(record["claim_grade_blockers"], [], record)
            self.assertTrue(record["claim_grade_contract"]["claim_grade_ready"], record)
            self.assertEqual(record["bench_rows"][0]["cargo_bench_target"], "deflate")

    def test_strict_rpolars_run_contract_can_emit_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            evidence_dir = pathlib.Path(tmp) / "evidence"
            checkout = write_fake_polars_checkout(root)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fake_cargo(bin_dir)
            csv_path, digest = write_rpolars_csv(root, rows=1)
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            }
            paper_source = {
                "paper_benchmark": "R-Polars",
                "paper_project": "Polars",
                "paper_version": "py-0.13.0",
            }
            input_contract = rpolars_input_contract(csv_path, digest, rows=1)
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "R-Polars",
                "--allocator",
                "unialloc",
                "--run-index",
                "1",
                "--cargo-bench-targets",
                "bench,csv,groupby,collect,take,sort",
                "--claim-grade-contract",
                "rpolars-paper-run",
                "--expected-cargo-bench-targets",
                "bench,csv,groupby,collect,take,sort",
                "--expected-libtest-bench-functions",
                "bench_unit",
                "--paper-source-contract-json",
                json.dumps(source_contract),
                "--paper-source-json",
                json.dumps(paper_source),
                "--rpolars-input-contract-json",
                json.dumps(input_contract),
                "--real-workload-dir",
                str(checkout),
                "--evidence-dir",
                str(evidence_dir),
                "--max-output-bytes",
                "4096",
                "--",
                "cargo",
                "bench",
                "--package",
                "polars",
                "--bench",
                "bench",
            ]
            with mock.patch.object(wrapper, "RPOLARS_CANONICAL_GROUPBY_ROWS", 1):
                with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "CSV_SRC": str(csv_path)}):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = wrapper.main(argv=argv)

            records = [
                json.loads(line)
                for line in stdout.getvalue().splitlines()
                if line.strip().startswith("{")
            ]
            self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
            record = records[-1]
            self.assertTrue(record["success"], record)
            self.assertTrue(record["claim_grade"], record)
            self.assertEqual(record["claim_grade_scope"], "rpolars-paper-equivalent-matrix-run")
            self.assertEqual(record["claim_grade_blockers"], [], record)
            self.assertTrue(record["claim_grade_contract"]["claim_grade_ready"], record)
            self.assertEqual(record["cargo_bench_targets"], ["bench", "csv", "groupby", "collect", "take", "sort"])

    def test_strict_rpolars_run_contract_rejects_non_cargo_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            evidence_dir = root / "evidence"
            checkout = write_fake_polars_checkout(root)
            csv_path, digest = write_rpolars_csv(root, rows=1)
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "R-Polars",
                "--allocator",
                "unialloc",
                "--run-index",
                "1",
                "--cargo-bench-targets",
                "bench,csv,groupby,collect,take,sort",
                "--claim-grade-contract",
                "rpolars-paper-run",
                "--expected-cargo-bench-targets",
                "bench,csv,groupby,collect,take,sort",
                "--expected-libtest-bench-functions",
                "bench_unit",
                "--paper-source-contract-json",
                json.dumps(source_contract),
                "--rpolars-input-contract-json",
                json.dumps(rpolars_input_contract(csv_path, digest, rows=1)),
                "--real-workload-dir",
                str(checkout),
                "--evidence-dir",
                str(evidence_dir),
                "--max-output-bytes",
                "4096",
                "--",
                sys.executable,
                "-c",
                "print('test bench_unit ... bench:       1,234 ns/iter (+/- 56)')",
            ]
            with mock.patch.object(wrapper, "RPOLARS_CANONICAL_GROUPBY_ROWS", 1):
                with mock.patch.dict(os.environ, {"CSV_SRC": str(csv_path)}):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = wrapper.main(argv=argv)

            records = [
                json.loads(line)
                for line in stdout.getvalue().splitlines()
                if line.strip().startswith("{")
            ]
            self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
            record = records[-1]
            self.assertTrue(record["success"], record)
            self.assertFalse(record["claim_grade"], record)
            self.assertTrue(
                any("not cargo bench" in blocker for blocker in record["claim_grade_blockers"]),
                record,
            )

    def test_strict_rjs_compiler_run_contract_can_emit_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = pathlib.Path(tmp) / "evidence"
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            }
            paper_source = {
                "paper_benchmark": "RJS-Compiler",
                "paper_project": "SWC",
                "paper_version": "v1.2.51",
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "RJS-Compiler",
                "--allocator",
                "unialloc",
                "--run-index",
                "1",
                "--cargo-bench-targets",
                "typescript",
                "--claim-grade-contract",
                "rjs-compiler-paper-run",
                "--expected-cargo-bench-targets",
                "typescript",
                "--expected-libtest-bench-functions",
                "parser,full_es5",
                "--paper-source-contract-json",
                json.dumps(source_contract),
                "--paper-source-json",
                json.dumps(paper_source),
                "--evidence-dir",
                str(evidence_dir),
                "--max-output-bytes",
                "4096",
                "--",
                sys.executable,
                "-c",
                (
                    "print('test parser ... bench:       1,234 ns/iter (+/- 56)'); "
                    "print('test full_es5 ... bench:       2,345 ns/iter (+/- 67)')"
                ),
            ]
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = wrapper.main(argv=argv)

            records = [
                json.loads(line)
                for line in stdout.getvalue().splitlines()
                if line.strip().startswith("{")
            ]
            self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
            record = records[-1]
            self.assertTrue(record["success"], record)
            self.assertTrue(record["claim_grade"], record)
            self.assertEqual(record["claim_grade_scope"], "rjs-compiler-paper-equivalent-matrix-run")
            self.assertEqual(record["claim_grade_blockers"], [], record)
            self.assertTrue(record["claim_grade_contract"]["claim_grade_ready"], record)
            self.assertEqual(record["cargo_bench_targets"], ["typescript"])

    def test_strict_rpolars_run_contract_rejects_missing_input_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = pathlib.Path(tmp) / "evidence"
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "R-Polars",
                "--allocator",
                "unialloc",
                "--run-index",
                "1",
                "--cargo-bench-targets",
                "bench,csv,groupby,collect,take,sort",
                "--claim-grade-contract",
                "rpolars-paper-run",
                "--expected-cargo-bench-targets",
                "bench,csv,groupby,collect,take,sort",
                "--expected-libtest-bench-functions",
                "bench_unit",
                "--paper-source-contract-json",
                json.dumps(source_contract),
                "--evidence-dir",
                str(evidence_dir),
                "--max-output-bytes",
                "4096",
                "--",
                sys.executable,
                "-c",
                "print('test bench_unit ... bench:       1,234 ns/iter (+/- 56)')",
            ]
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = wrapper.main(argv=argv)

            records = [
                json.loads(line)
                for line in stdout.getvalue().splitlines()
                if line.strip().startswith("{")
            ]
            self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
            record = records[-1]
            self.assertTrue(record["success"], record)
            self.assertFalse(record["claim_grade"], record)
            self.assertIn("R-Polars input provenance contract is missing", record["claim_grade_blockers"])

    def test_rpolars_contract_rejects_synthetic_csv_fixture(self) -> None:
        record = {
            "benchmark": "R-Polars",
            "dependency_env": {
                "polars_csv_fixture": {
                    "claim_grade": False,
                    "not_claim_grade_reason": "synthetic fixture is not paper input",
                }
            },
        }
        blockers = wrapper.rpolars_dependency_contract_blockers(record)
        self.assertIn("synthetic fixture is not paper input", blockers)

    def test_existing_rpolars_csv_can_use_claim_grade_input_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            checkout = root / "polars-checkout"
            (checkout / "polars" / "benches").mkdir(parents=True)
            (checkout / "polars" / "benches" / "groupby.rs").write_text("// groupby bench\n", encoding="utf-8")
            csv_path, digest = write_rpolars_csv(root, rows=1)
            input_contract = rpolars_input_contract(csv_path, digest, rows=1)
            args = argparse.Namespace(
                benchmark="R-Polars",
                real_workload_dir=str(checkout),
                rpolars_input_contract_json=json.dumps(input_contract),
            )
            env = {"CSV_SRC": str(csv_path)}

            with mock.patch.object(wrapper, "RPOLARS_CANONICAL_GROUPBY_ROWS", 1):
                record = wrapper.maybe_prepare_polars_csv_fixture(
                    args,
                    env,
                    requested_targets=["groupby"],
                )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertTrue(record["claim_grade"], record)
        self.assertNotIn("not_claim_grade_reason", record, record)
        self.assertEqual(record["selected_csv_src"], str(csv_path))

    def test_existing_rpolars_csv_rejects_fake_row_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            checkout = root / "polars-checkout"
            (checkout / "polars" / "benches").mkdir(parents=True)
            (checkout / "polars" / "benches" / "groupby.rs").write_text("// groupby bench\n", encoding="utf-8")
            csv_path, digest = write_rpolars_csv(root, rows=0)
            input_contract = rpolars_input_contract(csv_path, digest, rows=1)
            args = argparse.Namespace(
                benchmark="R-Polars",
                real_workload_dir=str(checkout),
                rpolars_input_contract_json=json.dumps(input_contract),
            )
            env = {"CSV_SRC": str(csv_path)}

            with mock.patch.object(wrapper, "RPOLARS_CANONICAL_GROUPBY_ROWS", 1):
                record = wrapper.maybe_prepare_polars_csv_fixture(
                    args,
                    env,
                    requested_targets=["groupby"],
                )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertFalse(record["claim_grade"], record)
        self.assertIn("row count", record["not_claim_grade_reason"])


if __name__ == "__main__":
    unittest.main()
