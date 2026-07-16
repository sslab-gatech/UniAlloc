#!/usr/bin/env python3
"""Tests for native-protocol macro build imports."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from evaluation.scripts import import_macro_baseline_builds as importer
from evaluation.scripts import run_primary_macro_allocator_baselines as campaign
from evaluation.scripts import type_isolation_suite_contract


class MacroBaselineBuildImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = campaign.load_campaign_contract(
            type_isolation_suite_contract.CURRENT_SUITE_PATH
        )
        self.protocol = campaign.build_protocol(
            self.contract, toolchain=campaign.DEFAULT_TOOLCHAIN
        )

    def source_record(
        self,
        *,
        target_id: str,
        variant: str,
        implementation_snapshot: pathlib.Path,
    ) -> dict[str, object]:
        target = self.contract.targets[target_id]
        record: dict[str, object] = {
            "protocol_fingerprint": "old-protocol",
            "target_fingerprint": "old-target",
            "build_fingerprint": "old-build",
            "build_id": "old-id",
            "build_path": "/old/build.json",
            "implementation_snapshot": str(implementation_snapshot),
            "target_id": target_id,
            "variant": variant,
            "source_commit": target.source_commit,
            "harness_binaries": {},
        }
        if variant in importer.ALIAS_VARIANTS:
            record.update(
                {
                    "base_build_variant": (
                        "system" if variant == "google_tcmalloc" else "mimalloc"
                    ),
                    "base_build_path": "/old/base/build.json",
                    "base_build_id": "old-base-id",
                }
            )
        if variant == "google_tcmalloc":
            record["tcmalloc_identity"] = {
                "revision": "revision",
                "library_sha256": "a" * 64,
                "hpaa_active": 1,
                "malloc_provider_is_self": 1,
                "label": "test",
            }
        return record

    def test_base_import_changes_only_the_approved_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source_path = root / "source.json"
            snapshot = root / "old-snapshot"
            destination_snapshot = root / "new-snapshot"
            source = self.source_record(
                target_id="collections",
                variant="jemalloc",
                implementation_snapshot=snapshot,
            )
            source_path.write_text(json.dumps(source), encoding="utf-8")
            source_sha256 = campaign.sha256_file(source_path)
            build_fingerprint = campaign.build_contract_fingerprint(
                self.protocol,
                target_id="collections",
                build_variant="jemalloc",
            )
            destination = importer.destination_record_path(
                root / "v4",
                target_id="collections",
                variant="jemalloc",
                build_fingerprint=build_fingerprint,
            )
            derived, changes = importer.derive_record(
                source,
                source_path=source_path,
                destination_path=destination,
                source_protocol_fingerprint="old-protocol",
                destination_protocol=self.protocol,
                import_id="import-id",
                destination_implementation_snapshot=destination_snapshot,
            )
        self.assertEqual(changes, importer.BASE_MUTATIONS)
        self.assertEqual(
            derived["implementation_snapshot"], str(destination_snapshot.resolve())
        )
        self.assertEqual(
            derived["compatibility_import"]["source_record"]["sha256"],
            source_sha256,
        )
        self.assertEqual(derived["build_id"], importer.recompute_build_id(derived))

    def test_alias_import_rebinds_only_the_native_base_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source_path = root / "source.json"
            source = self.source_record(
                target_id="swc",
                variant="google_tcmalloc",
                implementation_snapshot=root / "old-snapshot",
            )
            source_path.write_text(json.dumps(source), encoding="utf-8")
            fingerprint = campaign.variant_fingerprint(
                self.protocol,
                target_id="swc",
                variant="google_tcmalloc",
                tcmalloc_identity=source["tcmalloc_identity"],
            )
            destination = importer.destination_record_path(
                root / "v4",
                target_id="swc",
                variant="google_tcmalloc",
                build_fingerprint=fingerprint,
            )
            base = {"build_path": "/v4/system/build.json", "build_id": "new-base"}
            derived, changes = importer.derive_record(
                source,
                source_path=source_path,
                destination_path=destination,
                source_protocol_fingerprint="old-protocol",
                destination_protocol=self.protocol,
                import_id="import-id",
                destination_implementation_snapshot=root / "new-snapshot",
                base_record=base,
            )
        self.assertEqual(changes, importer.ALIAS_MUTATIONS)
        self.assertEqual(derived["base_build_path"], base["build_path"])
        self.assertEqual(derived["base_build_id"], base["build_id"])

    def test_source_record_inventory_requires_all_six_records_per_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for variant in importer.BASE_VARIANTS:
                path = root / "builds/collections" / variant / "build.json"
                path.parent.mkdir(parents=True)
                path.write_text("{}\n", encoding="utf-8")
            for variant in importer.ALIAS_VARIANTS:
                path = root / "builds/collections" / variant / "fingerprint/build.json"
                path.parent.mkdir(parents=True)
                path.write_text("{}\n", encoding="utf-8")
            records = importer.source_record_paths(root, ("collections",))
            self.assertEqual(len(records), 6)
            duplicate = (
                root
                / "builds/collections/google_tcmalloc/second-fingerprint/build.json"
            )
            duplicate.parent.mkdir(parents=True)
            duplicate.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(importer.ImportError, "count must be one"):
                importer.source_record_paths(root, ("collections",))

    def test_snapshot_copy_preserves_the_canonical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "v3/frozen-implementation/digest"
            source.mkdir(parents=True)
            (source / "unialloc").mkdir()
            payload = b"allocator"
            blob = source / "unialloc/src.rs"
            blob.write_bytes(payload)
            digest, count, size = campaign.redb_actix.canonical_stream(
                source, ("unialloc/src.rs",)
            )
            manifest = {
                "implementation_sha256": digest,
                "canonical_file_count": count,
                "canonical_size_bytes": size,
                "git_blobs": [
                    {
                        "path": "unialloc/src.rs",
                        "sha256": campaign.sha256_file(blob),
                        "size_bytes": len(payload),
                    }
                ],
            }
            (source / "snapshot.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            records = {
                ("collections", "unialloc"): {
                    "implementation_snapshot": str(source)
                }
            }
            destination, evidence = importer.materialize_snapshot_copy(
                root / "v4", records
            )
        self.assertEqual(evidence["canonical_sha256"], digest)
        self.assertEqual(evidence["canonical_file_count"], 1)
        self.assertEqual(destination.name, "digest")


if __name__ == "__main__":
    unittest.main()
