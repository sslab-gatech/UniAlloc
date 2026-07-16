#!/usr/bin/env python3
"""Tests for dependency-free immutable raw evidence publication."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evaluation.scripts import immutable_evidence as evidence


IDENTITY = {
    "suite_id": "suite-v1",
    "suite_manifest_sha256": "a" * 64,
    "target_id": "target",
    "harness_id": "harness",
    "variant": "unialloc",
    "phase": "measurement",
    "round": 1,
    "source_commit": "b" * 40,
    "implementation_revision": "c" * 40,
    "implementation_sha256": "d" * 64,
    "binary_sha256": "e" * 64,
}


class ImmutableEvidenceTests(unittest.TestCase):
    def candidate(self, artifact: Path, performance: float = 1.25) -> dict[str, object]:
        return {
            "evidence_schema_version": 1,
            "identity": dict(IDENTITY),
            "metrics": {
                "performance": performance,
                "performance_unit": "seconds",
                "peak_rss_mib": 4.0,
            },
            "artifacts": {"stdout": evidence.artifact_ref(artifact)},
        }

    def test_attempts_are_unique_and_artifacts_are_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commit = root / "records" / "target-harness-unialloc.json"
            first = evidence.begin_attempt(commit)
            second = evidence.begin_attempt(commit)
            self.assertNotEqual(first, second)
            artifact = first / "stdout.bin"
            artifact.write_bytes(b"evidence")
            reference = evidence.artifact_ref(artifact)
            self.assertEqual(str(artifact.resolve()), reference["path"])
            self.assertEqual(8, reference["bytes"])
            self.assertRegex(reference["sha256"], r"^[0-9a-f]{64}$")

    def test_record_publication_is_no_replace_and_exactly_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_path = root / "records" / "record.json"
            collects = 0

            def collect(attempt: Path) -> dict[str, object]:
                nonlocal collects
                collects += 1
                artifact = attempt / "stdout.bin"
                artifact.write_bytes(b"evidence")
                return self.candidate(artifact)

            validate = lambda value: evidence.validate_measurement_record(  # noqa: E731
                value, expected_identity=IDENTITY
            )
            first = evidence.run_or_reuse_json(record_path, collect, validate)
            second = evidence.run_or_reuse_json(record_path, collect, validate)
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(1, collects)
            self.assertEqual(first.record_sha256, second.record_sha256)
            self.assertEqual(1, record_path.stat().st_nlink)
            self.assertEqual(0, record_path.stat().st_mode & 0o222)
            self.assertEqual(
                [],
                list((record_path.parent / "attempts").rglob("candidate-record.json")),
            )
            original = record_path.read_bytes()
            record_path.chmod(0o644)
            record_path.write_bytes(b"{}\n")
            with self.assertRaises(evidence.ImmutableEvidenceError):
                evidence.run_or_reuse_json(record_path, collect, validate)
            record_path.write_bytes(original)
            record_path.chmod(0o444)
            self.assertEqual(original, record_path.read_bytes())

    def test_reuse_reloads_identity_metrics_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_path = root / "record.json"
            artifact_path: Path | None = None

            def collect(attempt: Path) -> dict[str, object]:
                nonlocal artifact_path
                artifact_path = attempt / "stdout.bin"
                artifact_path.write_bytes(b"evidence")
                return self.candidate(artifact_path)

            validate = lambda value: evidence.validate_measurement_record(  # noqa: E731
                value, expected_identity=IDENTITY
            )
            evidence.run_or_reuse_json(record_path, collect, validate)
            reused = evidence.run_or_reuse_json(record_path, collect, validate)
            self.assertTrue(reused.reused)
            assert artifact_path is not None
            artifact_path.write_bytes(b"tampered")
            with self.assertRaises(evidence.ImmutableEvidenceError):
                evidence.run_or_reuse_json(record_path, collect, validate)

    def test_immutable_bytes_reject_different_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "suite-manifest.json"
            first = evidence.persist_immutable_bytes(path, b"first\n")
            second = evidence.persist_immutable_bytes(path, b"first\n")
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            with self.assertRaises(evidence.ImmutableEvidenceError):
                evidence.persist_immutable_bytes(path, b"second\n")
            self.assertEqual(b"first\n", path.read_bytes())

    def test_interrupted_attempt_does_not_block_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            commit = Path(temporary) / "records" / "record.json"
            abandoned = evidence.begin_attempt(commit)
            (abandoned / "partial.stdout").write_bytes(b"partial")

            def collect(attempt: Path) -> dict[str, object]:
                artifact = attempt / "stdout.bin"
                artifact.write_bytes(b"complete")
                return self.candidate(artifact)

            committed = evidence.run_or_reuse_json(
                commit,
                collect,
                lambda value: evidence.validate_measurement_record(
                    value, expected_identity=IDENTITY
                ),
            )
            self.assertTrue(commit.is_file())
            self.assertFalse(committed.reused)
            self.assertTrue(abandoned.is_dir())

    def test_manifest_binding_is_digest_checked_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "suite.json"
            destination = root / "raw" / "suite-manifest.json"
            source.write_bytes(b'{"suite_id":"suite-v1"}\n')
            digest = evidence.sha256_file(source)
            first = evidence.bind_immutable_file(
                source, destination, expected_sha256=digest
            )
            second = evidence.bind_immutable_file(
                source, destination, expected_sha256=digest
            )
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            source.write_bytes(b'{"suite_id":"changed"}\n')
            with self.assertRaises(evidence.ImmutableEvidenceError):
                evidence.bind_immutable_file(
                    source, destination, expected_sha256=digest
                )


if __name__ == "__main__":
    unittest.main()
