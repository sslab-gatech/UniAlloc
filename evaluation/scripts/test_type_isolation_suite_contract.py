#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from evaluation.scripts import type_isolation_suite_contract as contract


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_SUITE = ROOT / "evaluation/config/type_isolation_primary_suite.json"
CURRENT_SUITE = (
    ROOT / "evaluation/config/type_isolation_primary_suite_v5_ce8af7b.json"
)


class TypeIsolationSuiteContractTests(unittest.TestCase):
    def test_historical_suite_is_preserved_byte_for_byte(self) -> None:
        self.assertEqual(
            "ba386a779e45e0884e02cf8825c69985d1b5cf68d57366af78e7f77cfa8f6ccb",
            hashlib.sha256(HISTORICAL_SUITE.read_bytes()).hexdigest(),
        )

    def test_current_suite_freezes_head_rounds_and_publication_namespaces(self) -> None:
        suite = contract.load_suite_contract(CURRENT_SUITE)
        self.assertEqual(
            "ce8af7b89a5cba9a9b3f57d9b02bb0c8cb5c3503",
            suite.implementation_revision,
        )
        self.assertEqual(
            "9deea74eaa2580ec1a0a57b001edfe9fa714428eaf0c211ee836bb0ddb16f178",
            suite.implementation_sha256,
        )
        self.assertEqual(137, suite.implementation_file_count)
        self.assertEqual(5_297_425, suite.implementation_size_bytes)
        self.assertEqual(1, suite.warmup_rounds)
        self.assertEqual(3, suite.measured_rounds)
        self.assertEqual(
            ROOT / "evaluation/raw/type-isolation-primary-v5-ce8af7b",
            suite.publication_root,
        )
        self.assertEqual(
            ROOT / "evaluation/raw/type-isolation-primary-v5-ce8af7b/targets",
            suite.target_results_dir,
        )
        self.assertEqual(
            ROOT / "benchmark-results/type-isolation-primary-v5-ce8af7b.json",
            suite.assembled_result,
        )
        self.assertEqual(
            suite.publication_root / "campaigns/redb-actix",
            suite.runner_raw_dir("redb-actix"),
        )
        self.assertEqual(contract.HOST_PRIMARY_MEASUREMENT_LOCK, suite.measurement_lock)
        self.assertEqual(
            set(contract.TYPE_ISOLATION_PROTOCOL_FILES),
            set(contract.verify_protocol_files(suite)),
        )

    def test_runtime_environment_is_allowlisted_and_records_libc_rseq(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmpdir = Path(temporary) / "runtime-tmp"
            inherited = {
                "PATH": "/usr/bin",
                "HOME": "/home/test",
                "USER": "test",
                "LOGNAME": "test",
                "SHELL": "/bin/sh",
                "TERM": "xterm",
                "TZ": "UTC",
                "LD_PRELOAD": "/tmp/wrong.so",
                "LD_LIBRARY_PATH": "/tmp/wrong-lib",
                "MALLOC_CONF": "background_thread:true",
                "GLIBC_TUNABLES": "glibc.pthread.rseq=0",
                "RUSTFLAGS": "-Ctarget-cpu=native",
                "UNIALLOC_LOWERING_POLICY_FLAGS": "99",
                "MIMALLOC_ALLOW_THP": "1",
                "TCMALLOC_SAMPLE_PARAMETER": "1",
                "SECRET_TOKEN": "do-not-copy",
            }
            environment = contract.clean_runtime_environment(
                tmpdir, source=inherited, overrides={"RAYON_NUM_THREADS": "1"}
            )
            self.assertEqual(
                {
                    "PATH",
                    "HOME",
                    "USER",
                    "LOGNAME",
                    "SHELL",
                    "TERM",
                    "TZ",
                    "LANG",
                    "LC_ALL",
                    "TMPDIR",
                    "RAYON_NUM_THREADS",
                },
                set(environment),
            )
            self.assertEqual("C", environment["LANG"])
            self.assertEqual("C", environment["LC_ALL"])
            self.assertEqual(str(tmpdir.resolve()), environment["TMPDIR"])
            self.assertTrue(tmpdir.is_dir())
            record = contract.runtime_environment_record(environment)
            self.assertEqual("libc_default", record["rseq_policy"])
            self.assertEqual(environment, record["environment"])
            self.assertFalse(record["glibc_tunables_present"])
            self.assertRegex(record["environment_sha256"], r"^[0-9a-f]{64}$")

    def test_current_suite_declares_libc_default_rseq_for_redb_and_actix(self) -> None:
        suite = contract.load_suite_contract(CURRENT_SUITE)
        targets = {row["id"]: row for row in suite.manifest["targets"]}
        self.assertEqual("libc_default", targets["redb"]["rseq_policy"])
        self.assertEqual("libc_default", targets["actix_web"]["rseq_policy"])

    def test_suite_manifest_binding_is_immutable_and_verifiable(self) -> None:
        suite = contract.load_suite_contract(CURRENT_SUITE)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "suite-manifest.json"
            first = contract.bind_suite_manifest(suite, destination=destination)
            second = contract.bind_suite_manifest(suite, destination=destination)
            verified = contract.verify_suite_manifest_binding(
                suite, path=destination
            )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(suite.manifest_sha256, verified["sha256"])

    def test_historical_suite_loads_only_through_its_exact_digest(self) -> None:
        suite = contract.load_suite_contract(HISTORICAL_SUITE)
        self.assertEqual(5, suite.measured_rounds)
        self.assertEqual(
            ROOT / "evaluation/raw/type-isolation-primary-v1/targets",
            suite.target_results_dir,
        )
        self.assertEqual(
            ROOT / "benchmark-results/type-isolation-primary-v1.json",
            suite.assembled_result,
        )

    def test_invalid_round_and_namespace_contracts_fail_closed(self) -> None:
        value = json.loads(CURRENT_SUITE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "suite.json"
            for mutation in (
                lambda row: row["measurement_contract"].update(
                    measured_rounds=0
                ),
                lambda row: row["publication"].update(namespace="../escape"),
            ):
                candidate = json.loads(json.dumps(value))
                mutation(candidate)
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.subTest(candidate=candidate["publication"]):
                    with self.assertRaises(contract.SuiteContractError):
                        contract.load_suite_contract(path)

    def test_protocol_digest_drift_fails_before_suite_binding(self) -> None:
        value = json.loads(CURRENT_SUITE.read_text(encoding="utf-8"))
        relative = contract.TYPE_ISOLATION_PROTOCOL_FILES[0]
        value["protocol_files"][relative] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "suite.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            suite = contract.load_suite_contract(path)
            with self.assertRaisesRegex(
                contract.SuiteContractError, "protocol file digest mismatch"
            ):
                contract.bind_suite_manifest(
                    suite, destination=Path(temporary) / "bound.json"
                )


if __name__ == "__main__":
    unittest.main()
