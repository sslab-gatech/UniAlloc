#!/usr/bin/env python3
"""Tests for fail-closed Type Isolation source-audit materialization."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from evaluation.scripts import materialize_type_isolation_source_audit as materializer


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MaterializeSourceAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def contract(self, suite_path: Path, implementation_sha256: str = "a" * 64):
        return SimpleNamespace(
            suite_id="test-suite",
            manifest_sha256=digest(suite_path),
            manifest={
                "targets": [
                    {
                        "id": "redb",
                        "source": {"commit": "1" * 40, "ref": "v1"},
                    }
                ]
            },
            implementation_revision="2" * 40,
            implementation_sha256=implementation_sha256,
            implementation_file_count=1,
            implementation_size_bytes=1,
        )

    def base_view(self, suite_path: Path, raw_dir: Path) -> dict[str, object]:
        return {
            "target_id": "redb",
            "suite_id": "test-suite",
            "suite_manifest_sha256": digest(suite_path),
            "suite_manifest_path": str(suite_path),
            "source": {},
            "builds": {
                variant: str(raw_dir / "builds" / "redb" / variant / "build.json")
                for variant in materializer.VARIANTS
            },
            "harnesses": [{"id": "h", "gates": {"source_audit_retained": True}}],
        }

    def test_materializes_content_addressed_evidence_and_only_updates_view(self) -> None:
        suite_path = self.root / "suite.json"
        suite_path.write_text("{}\n", encoding="utf-8")
        raw_dir = self.root / "raw"
        view_path = raw_dir / "results" / "redb.json"
        write_json(view_path, self.base_view(suite_path, raw_dir))
        write_json(raw_dir / "sources" / "redb" / "source.json", {})
        before = json.loads(view_path.read_text(encoding="utf-8"))
        contract = self.contract(suite_path)

        with (
            mock.patch.object(
                materializer,
                "validate_source",
                return_value=({"source_commit": "1" * 40}, {"record": "source"}),
            ),
            mock.patch.object(
                materializer,
                "validate_implementation",
                return_value={"sha256": "a" * 64},
            ),
            mock.patch.object(
                materializer,
                "validate_variant",
                side_effect=[
                    ({"build_record": "u"}, None),
                    ({"build_record": "p"}, None),
                    ({"build_record": "t"}, None),
                    ({"build_record": "u"}, None),
                    ({"build_record": "p"}, None),
                    ({"build_record": "t"}, None),
                ],
            ),
        ):
            first_path, first_digest = materializer.materialize_target(
                contract,
                raw_dir,
                view_path,
                "redb",
                audit_root=self.root / "audits",
            )
            second_path, second_digest = materializer.materialize_target(
                contract,
                raw_dir,
                view_path,
                "redb",
                audit_root=self.root / "audits",
            )

        self.assertEqual(first_path, second_path)
        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first_digest, digest(first_path))
        self.assertFalse(stat.S_IMODE(first_path.stat().st_mode) & 0o222)
        after = json.loads(view_path.read_text(encoding="utf-8"))
        evidence = after.pop("evidence")
        self.assertEqual(before, after)
        self.assertEqual(evidence["source_audit"], str(first_path))
        self.assertEqual(evidence["source_audit_sha256"], first_digest)
        self.assertEqual(evidence["source_audit_bytes"], first_path.stat().st_size)

    def test_refuses_read_only_published_target_record(self) -> None:
        suite_path = self.root / "suite.json"
        suite_path.write_text("{}\n", encoding="utf-8")
        raw_dir = self.root / "raw"
        view_path = raw_dir / "results" / "redb.json"
        write_json(view_path, self.base_view(suite_path, raw_dir))
        view_path.chmod(0o444)
        before = view_path.read_bytes()
        with self.assertRaisesRegex(materializer.MaterializationError, "read-only"):
            materializer.materialize_target(
                self.contract(suite_path), raw_dir, view_path, "redb"
            )
        self.assertEqual(before, view_path.read_bytes())

    def test_source_record_is_bound_to_clean_git_checkout(self) -> None:
        checkout = self.root / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
        (checkout / "README").write_text("source\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(checkout), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "source"], check=True)
        commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        tree = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"], text=True).strip()
        source = {
            "target_id": "redb",
            "repository": "local",
            "source_ref": "v1",
            "source_commit": commit,
            "tree": tree,
            "checkout": str(checkout),
            "status": "",
            "cargo_lock_sha256": None,
        }
        source_path = self.root / "source.json"
        write_json(source_path, source)
        contract = SimpleNamespace(manifest_sha256="f" * 64)
        target_spec = {"source": {"commit": commit, "ref": "v1"}}
        materializer.validate_source(
            contract, target_spec, {"source": source}, source_path, "redb"
        )
        (checkout / "README").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(materializer.MaterializationError, "status"):
            materializer.validate_source(
                contract, target_spec, {"source": source}, source_path, "redb"
            )

    def test_variant_policy_mismatch_fails_before_artifact_promotion(self) -> None:
        raw_dir = self.root / "raw"
        build_path = raw_dir / "builds" / "redb" / "typed_plain" / "build.json"
        record = {
            "target_id": "redb",
            "variant": "typed_plain",
            "source_commit": "1" * 40,
            "implementation_revision": "2" * 40,
            "implementation_sha256": "a" * 64,
            "frozen_implementation_sha256": "a" * 64,
            "unialloc_implementation_sha256": "a" * 64,
            "stats_enabled": False,
            "lowering_policy_flags": 1,
            "actual_mir_rewrite": True,
        }
        write_json(build_path, record)
        build_path.chmod(0o444)
        contract = SimpleNamespace(
            implementation_revision="2" * 40,
            implementation_sha256="a" * 64,
        )
        with self.assertRaisesRegex(materializer.MaterializationError, "policy flag"):
            materializer.validate_variant(
                contract,
                raw_dir,
                "redb",
                {"source_commit": "1" * 40},
                "typed_plain",
                build_path,
            )

    def test_exact_child_rejects_evidence_path_escape(self) -> None:
        parent = self.root / "root"
        parent.mkdir()
        with self.assertRaisesRegex(materializer.MaterializationError, "escapes"):
            materializer.exact_child(self.root / "elsewhere", parent, "audit")

    def test_psr_snapshot_recomputes_implementation_digest(self) -> None:
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        (snapshot / "one.rs").write_bytes(b"x")
        stream = hashlib.sha256(b"one.rs\0x\0").hexdigest()
        manifest_path = self.root / "frozen-unialloc.json"
        write_json(
            manifest_path,
            {
                "path": str(snapshot),
                "allocator_revision": "2" * 40,
                "source_head": "2" * 40,
                "unialloc_implementation_sha256": stream,
                "implementation_file_count": 1,
                "implementation_total_bytes": 1,
                "implementation_files": ["one.rs"],
                "campaign_snapshot_sha256": "3" * 64,
            },
        )
        contract = SimpleNamespace(
            implementation_revision="2" * 40,
            implementation_sha256=stream,
            implementation_file_count=1,
            implementation_size_bytes=1,
        )
        evidence = materializer.validate_implementation(
            contract,
            {
                "implementation_revision": "2" * 40,
                "implementation_sha256": stream,
                "frozen_snapshot_record": str(manifest_path),
            },
        )
        self.assertEqual(evidence["sha256"], stream)
        (snapshot / "one.rs").write_bytes(b"y")
        with self.assertRaisesRegex(materializer.MaterializationError, "digest"):
            materializer.validate_implementation(
                contract,
                {
                    "implementation_revision": "2" * 40,
                    "implementation_sha256": stream,
                    "frozen_snapshot_record": str(manifest_path),
                },
            )

    def test_psr_variant_policy_mismatch_fails_closed(self) -> None:
        raw_dir = self.root / "raw"
        build_path = raw_dir / "builds" / "polars" / "typed_plain" / "build.json"
        write_json(
            build_path,
            {
                "target_id": "polars",
                "variant": "typed_plain",
                "source_commit": "1" * 40,
                "source_ref": "v1",
                "implementation_revision": "2" * 40,
                "allocator_revision": "2" * 40,
                "implementation_sha256": "a" * 64,
                "unialloc_implementation_sha256": "a" * 64,
                "campaign_snapshot_sha256": "b" * 64,
                "success": True,
                "exit_code": 0,
                "timed_out": False,
                "stats_enabled": False,
                "policy_flags": 1,
                "actual_mir_provenance": True,
            },
        )
        build_path.chmod(0o444)
        contract = SimpleNamespace(
            implementation_revision="2" * 40,
            implementation_sha256="a" * 64,
        )
        with self.assertRaisesRegex(materializer.MaterializationError, "policy flag"):
            materializer.validate_psr_variant(
                contract,
                raw_dir,
                "polars",
                {"head": "1" * 40, "source_ref": "v1"},
                {"campaign_snapshot_sha256": "b" * 64},
                "typed_plain",
                build_path,
            )

    def test_compatible_predecessor_must_be_zero_measurement_materialization(self) -> None:
        good = self.root / "good.json"
        committed = materializer.immutable_evidence.persist_immutable_json(
            good,
            {
                "evidence_kind": "type_isolation_source_audit_compatibility_materialization",
                "target_id": "redb",
                "derivation": {
                    "measurement_records_read": 0,
                    "measurement_records_modified": 0,
                },
                "gates": {"source_audit_retained": True},
            },
        )
        materializer.validate_compatible_predecessor(
            str(good), committed.record_sha256, "redb"
        )
        bad = self.root / "bad.json"
        bad.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            materializer.MaterializationError, "compatible predecessor"
        ):
            materializer.validate_compatible_predecessor(
                str(bad), digest(bad), "redb"
            )


if __name__ == "__main__":
    unittest.main()
