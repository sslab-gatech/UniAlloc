#!/usr/bin/env python3
"""Import immutable macro baseline builds into the current native protocol.

The importer changes only protocol-derived build identity fields and record
paths. All compiled artifacts, source worktrees, command logs, allocator
proofs, and implementation snapshots remain immutable dependencies of the
source campaign.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from collections.abc import Mapping, Sequence
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
for _path in (ROOT, SCRIPT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evaluation.scripts import immutable_evidence  # noqa: E402
from evaluation.scripts import (  # noqa: E402
    run_primary_macro_allocator_baselines as campaign,
)
from evaluation.scripts import type_isolation_suite_contract  # noqa: E402


SCHEMA_VERSION = 1
BASE_VARIANTS = ("unialloc", "mimalloc", "system", "jemalloc")
ALIAS_VARIANTS = ("mimalloc_no_thp", "google_tcmalloc")
PUBLIC_VARIANTS = (
    "unialloc",
    "mimalloc",
    "mimalloc_no_thp",
    "google_tcmalloc",
    "jemalloc",
)
BASE_MUTATIONS = frozenset(
    {
        "protocol_fingerprint",
        "target_fingerprint",
        "build_fingerprint",
        "build_id",
        "build_path",
        "implementation_snapshot",
        "compatibility_import",
    }
)
ALIAS_MUTATIONS = BASE_MUTATIONS | {"base_build_path", "base_build_id"}


class ImportError(RuntimeError):
    """Raised when a source record or derived import fails closed."""


def canonical_sha256(value: Any) -> str:
    return campaign.canonical_json_sha256(value)


def load_object(path: pathlib.Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ImportError(f"cannot load {context}: {path}") from error
    if not isinstance(value, dict):
        raise ImportError(f"{context} must be an object: {path}")
    return value


def changed_top_level_keys(
    source: Mapping[str, Any], destination: Mapping[str, Any]
) -> frozenset[str]:
    return frozenset(
        key
        for key in source.keys() | destination.keys()
        if source.get(key) != destination.get(key)
    )


def source_protocol(
    source_raw: pathlib.Path, *, current: campaign.Protocol
) -> tuple[campaign.Protocol, pathlib.Path]:
    manifests = sorted((source_raw / "campaigns").glob("*/campaign.json"))
    if len(manifests) != 1:
        raise ImportError(
            "source raw directory must contain exactly one protocol manifest"
        )
    try:
        protocol = campaign.load_reusable_protocol_manifest(
            manifests[0], current=current, raw_dir=source_raw
        )
    except campaign.CampaignError as error:
        raise ImportError(str(error)) from error
    if protocol.fingerprint == current.fingerprint:
        raise ImportError("source and destination protocols must differ")
    return protocol, manifests[0]


def source_record_paths(
    source_raw: pathlib.Path, target_ids: Sequence[str]
) -> dict[tuple[str, str], pathlib.Path]:
    records: dict[tuple[str, str], pathlib.Path] = {}
    for target_id in target_ids:
        for variant in BASE_VARIANTS:
            path = source_raw / "builds" / target_id / variant / "build.json"
            if not path.is_file():
                raise ImportError(f"source base build is missing: {path}")
            records[(target_id, variant)] = path.resolve()
        for variant in ALIAS_VARIANTS:
            root = source_raw / "builds" / target_id / variant
            matches = sorted(root.glob("*/build.json"))
            if len(matches) != 1:
                raise ImportError(
                    f"source alias build count must be one: {target_id}/{variant}"
                )
            records[(target_id, variant)] = matches[0].resolve()
    expected = len(target_ids) * (len(BASE_VARIANTS) + len(ALIAS_VARIANTS))
    if len(records) != expected:
        raise ImportError(
            f"source formal build set is incomplete: {len(records)} != {expected}"
        )
    return records


def destination_record_path(
    destination_raw: pathlib.Path,
    *,
    target_id: str,
    variant: str,
    build_fingerprint: str,
) -> pathlib.Path:
    root = destination_raw / "builds" / target_id / variant
    if variant in ALIAS_VARIANTS:
        return root / build_fingerprint / "build.json"
    return root / "build.json"


def recompute_build_id(record: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            "build_fingerprint": record["build_fingerprint"],
            "target_id": record["target_id"],
            "variant": record["variant"],
            "source_commit": record["source_commit"],
            "harness_binaries": record["harness_binaries"],
        }
    )


def import_contract_id(
    *,
    source_raw: pathlib.Path,
    destination_raw: pathlib.Path,
    source: campaign.Protocol,
    destination: campaign.Protocol,
    suite: type_isolation_suite_contract.SuiteContract,
) -> str:
    return canonical_sha256(
        {
            "schema_version": SCHEMA_VERSION,
            "source_raw": str(source_raw.resolve()),
            "destination_raw": str(destination_raw.resolve()),
            "source_protocol_fingerprint": source.fingerprint,
            "destination_protocol_fingerprint": destination.fingerprint,
            "suite_manifest_sha256": suite.manifest_sha256,
            "importer_sha256": campaign.sha256_file(pathlib.Path(__file__).resolve()),
            "permitted_base_mutations": sorted(BASE_MUTATIONS),
            "permitted_alias_mutations": sorted(ALIAS_MUTATIONS),
        }
    )


def derive_record(
    source_record: Mapping[str, Any],
    *,
    source_path: pathlib.Path,
    destination_path: pathlib.Path,
    source_protocol_fingerprint: str,
    destination_protocol: campaign.Protocol,
    import_id: str,
    destination_implementation_snapshot: pathlib.Path,
    base_record: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], frozenset[str]]:
    record = json.loads(json.dumps(source_record))
    target_id = str(record.get("target_id"))
    variant = str(record.get("variant"))
    if record.get("protocol_fingerprint") != source_protocol_fingerprint:
        raise ImportError(f"source record protocol mismatch: {source_path}")
    if variant in ALIAS_VARIANTS:
        google_identity = (
            record.get("tcmalloc_identity")
            if variant == "google_tcmalloc"
            else None
        )
        build_fingerprint = campaign.variant_fingerprint(
            destination_protocol,
            target_id=target_id,
            variant=variant,
            tcmalloc_identity=google_identity,
        )
    else:
        build_fingerprint = campaign.build_contract_fingerprint(
            destination_protocol,
            target_id=target_id,
            build_variant=variant,
        )
    expected_path = destination_record_path(
        destination_path.parents[3]
        if variant not in ALIAS_VARIANTS
        else destination_path.parents[4],
        target_id=target_id,
        variant=variant,
        build_fingerprint=build_fingerprint,
    )
    if expected_path.resolve() != destination_path.resolve():
        raise ImportError(f"destination build path mismatch: {destination_path}")
    record.update(
        {
            "protocol_fingerprint": destination_protocol.fingerprint,
            "target_fingerprint": campaign.target_fingerprint(
                destination_protocol, target_id
            ),
            "build_fingerprint": build_fingerprint,
            "build_path": str(destination_path.resolve()),
            "implementation_snapshot": str(
                destination_implementation_snapshot.resolve()
            ),
        }
    )
    if variant in ALIAS_VARIANTS:
        if base_record is None:
            raise ImportError(
                f"alias import lacks its native base: {target_id}/{variant}"
            )
        record["base_build_path"] = str(base_record["build_path"])
        record["base_build_id"] = str(base_record["build_id"])
    record["compatibility_import"] = {
        "schema_version": SCHEMA_VERSION,
        "import_id": import_id,
        "source_protocol_fingerprint": source_protocol_fingerprint,
        "destination_protocol_fingerprint": destination_protocol.fingerprint,
        "source_record": immutable_evidence.artifact_ref(source_path),
        "permitted_mutations": sorted(
            ALIAS_MUTATIONS if variant in ALIAS_VARIANTS else BASE_MUTATIONS
        ),
    }
    record["build_id"] = recompute_build_id(record)
    changes = changed_top_level_keys(source_record, record)
    expected_changes = ALIAS_MUTATIONS if variant in ALIAS_VARIANTS else BASE_MUTATIONS
    if changes != expected_changes:
        raise ImportError(
            f"derived record mutation set mismatch for {target_id}/{variant}: "
            f"{sorted(changes)} != {sorted(expected_changes)}"
        )
    return record, changes


def persist_record(path: pathlib.Path, record: Mapping[str, Any]) -> pathlib.Path:
    try:
        return immutable_evidence.persist_immutable_json(path, record).path
    except immutable_evidence.ImmutableEvidenceError as error:
        raise ImportError(str(error)) from error


def materialize_snapshot_copy(
    destination_raw: pathlib.Path, records: Mapping[tuple[str, str], Mapping[str, Any]]
) -> tuple[pathlib.Path, dict[str, Any]]:
    paths = {
        pathlib.Path(str(record["implementation_snapshot"])).resolve()
        for record in records.values()
    }
    if len(paths) != 1:
        raise ImportError("source records disagree on implementation snapshot")
    source = next(iter(paths))
    if not source.is_dir():
        raise ImportError(f"source implementation snapshot is missing: {source}")
    destination = destination_raw / "frozen-implementation" / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ImportError(
            "destination implementation snapshot cannot be a symlink: "
            f"{destination}"
        )
    if not destination.exists():
        shutil.copytree(source, destination)
    source_manifest = load_object(source / "snapshot.json", "source snapshot manifest")
    destination_manifest = load_object(
        destination / "snapshot.json", "destination snapshot manifest"
    )
    rows = source_manifest.get("git_blobs")
    if not isinstance(rows, list) or not rows:
        raise ImportError("source implementation snapshot has no Git blob inventory")
    relative_paths = [
        str(row["path"])
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    ]
    if len(relative_paths) != len(rows) or destination_manifest != source_manifest:
        raise ImportError("destination implementation snapshot manifest differs")
    source_identity = campaign.redb_actix.canonical_stream(source, relative_paths)
    destination_identity = campaign.redb_actix.canonical_stream(
        destination, relative_paths
    )
    expected_identity = (
        source_manifest.get("implementation_sha256"),
        source_manifest.get("canonical_file_count"),
        source_manifest.get("canonical_size_bytes"),
    )
    if source_identity != expected_identity or destination_identity != source_identity:
        raise ImportError("implementation snapshot canonical identity mismatch")
    return destination.resolve(), {
        "source": str(source),
        "destination": str(destination.resolve()),
        "source_manifest": immutable_evidence.artifact_ref(source / "snapshot.json"),
        "destination_manifest": immutable_evidence.artifact_ref(
            destination / "snapshot.json"
        ),
        "canonical_sha256": source_identity[0],
        "canonical_file_count": source_identity[1],
        "canonical_size_bytes": source_identity[2],
    }


def build_index(
    destination_raw: pathlib.Path,
    records: Mapping[tuple[str, str], Mapping[str, Any]],
) -> pathlib.Path:
    rows = [
        {
            "target_id": target_id,
            "variant": variant,
            "build_id": records[(target_id, variant)]["build_id"],
            "build_path": records[(target_id, variant)]["build_path"],
            "harness_binaries": records[(target_id, variant)]["harness_binaries"],
            "compatibility_import": records[(target_id, variant)][
                "compatibility_import"
            ],
        }
        for target_id in sorted({key[0] for key in records})
        for variant in PUBLIC_VARIANTS
    ]
    path = destination_raw / "import-build-index.json"
    campaign.write_json(path, {"schema_version": 1, "builds": rows})
    return path


def run_import(
    *,
    source_raw: pathlib.Path,
    destination_raw: pathlib.Path,
    suite_path: pathlib.Path,
    toolchain: str,
) -> pathlib.Path:
    source_raw = source_raw.resolve()
    destination_raw = destination_raw.resolve()
    if source_raw == destination_raw:
        raise ImportError("source and destination raw directories must differ")
    contract = campaign.load_campaign_contract(suite_path)
    current = campaign.build_protocol(contract, toolchain=toolchain)
    old, old_manifest = source_protocol(source_raw, current=current)
    source_paths = source_record_paths(source_raw, tuple(contract.targets))
    source_records = {
        key: load_object(path, "source build record")
        for key, path in source_paths.items()
    }
    import_id = import_contract_id(
        source_raw=source_raw,
        destination_raw=destination_raw,
        source=old,
        destination=current,
        suite=contract.suite,
    )
    destination_raw.mkdir(parents=True, exist_ok=True)
    try:
        suite_binding = type_isolation_suite_contract.bind_suite_manifest(
            contract.suite, destination=destination_raw / "suite-manifest.json"
        )
        type_isolation_suite_contract.verify_suite_manifest_binding(
            contract.suite, path=pathlib.Path(str(suite_binding["path"]))
        )
    except type_isolation_suite_contract.SuiteContractError as error:
        raise ImportError(str(error)) from error
    campaign.ensure_campaign_manifest(
        destination_raw,
        current,
        reuse=True,
        suite_binding=suite_binding,
    )
    destination_snapshot, snapshot_dependency = materialize_snapshot_copy(
        destination_raw, source_records
    )
    imported: dict[tuple[str, str], dict[str, Any]] = {}
    manifest_rows: list[dict[str, Any]] = []
    for target_id in contract.targets:
        for variant in BASE_VARIANTS:
            source_path = source_paths[(target_id, variant)]
            fingerprint = campaign.build_contract_fingerprint(
                current, target_id=target_id, build_variant=variant
            )
            destination_path = destination_record_path(
                destination_raw,
                target_id=target_id,
                variant=variant,
                build_fingerprint=fingerprint,
            )
            record, changes = derive_record(
                source_records[(target_id, variant)],
                source_path=source_path,
                destination_path=destination_path,
                source_protocol_fingerprint=old.fingerprint,
                destination_protocol=current,
                import_id=import_id,
                destination_implementation_snapshot=destination_snapshot,
            )
            persist_record(destination_path, record)
            imported[(target_id, variant)] = record
            manifest_rows.append(
                {
                    "target_id": target_id,
                    "variant": variant,
                    "source": immutable_evidence.artifact_ref(source_path),
                    "destination": immutable_evidence.artifact_ref(destination_path),
                    "source_build_id": source_records[(target_id, variant)][
                        "build_id"
                    ],
                    "destination_build_id": record["build_id"],
                    "mutated_fields": sorted(changes),
                }
            )
        for variant in ALIAS_VARIANTS:
            source_path = source_paths[(target_id, variant)]
            source_record = source_records[(target_id, variant)]
            google_identity = (
                source_record.get("tcmalloc_identity")
                if variant == "google_tcmalloc"
                else None
            )
            fingerprint = campaign.variant_fingerprint(
                current,
                target_id=target_id,
                variant=variant,
                tcmalloc_identity=google_identity,
            )
            destination_path = destination_record_path(
                destination_raw,
                target_id=target_id,
                variant=variant,
                build_fingerprint=fingerprint,
            )
            base_variant = str(source_record["base_build_variant"])
            record, changes = derive_record(
                source_record,
                source_path=source_path,
                destination_path=destination_path,
                source_protocol_fingerprint=old.fingerprint,
                destination_protocol=current,
                import_id=import_id,
                destination_implementation_snapshot=destination_snapshot,
                base_record=imported[(target_id, base_variant)],
            )
            persist_record(destination_path, record)
            imported[(target_id, variant)] = record
            manifest_rows.append(
                {
                    "target_id": target_id,
                    "variant": variant,
                    "source": immutable_evidence.artifact_ref(source_path),
                    "destination": immutable_evidence.artifact_ref(destination_path),
                    "source_build_id": source_record["build_id"],
                    "destination_build_id": record["build_id"],
                    "mutated_fields": sorted(changes),
                }
            )
    if len(imported) != 42 or len(manifest_rows) != 42:
        raise ImportError("native protocol import did not produce exactly 42 records")
    index_path = build_index(destination_raw, imported)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "import_id": import_id,
        "importer": immutable_evidence.artifact_ref(pathlib.Path(__file__).resolve()),
        "suite_manifest_sha256": contract.suite.manifest_sha256,
        "source_raw": str(source_raw),
        "destination_raw": str(destination_raw),
        "source_protocol": {
            "fingerprint": old.fingerprint,
            "manifest": immutable_evidence.artifact_ref(old_manifest),
            "runner": dict(old.payload["runner"]),
        },
        "destination_protocol": {
            "fingerprint": current.fingerprint,
            "runner": dict(current.payload["runner"]),
        },
        "import_execution": {
            "processes_started": 0,
            "policy": "pure-python-file-derivation-with-no-build-or-command-execution",
        },
        "artifact_path_policy": (
            "compiled artifacts, source worktrees, and command logs retain their "
            "absolute immutable v3 paths"
        ),
        "permitted_base_mutations": sorted(BASE_MUTATIONS),
        "permitted_alias_mutations": sorted(ALIAS_MUTATIONS),
        "implementation_snapshot_copy": snapshot_dependency,
        "record_count": len(manifest_rows),
        "records": manifest_rows,
        "source_record_set_sha256": canonical_sha256(
            [row["source"] for row in manifest_rows]
        ),
        "destination_record_set_sha256": canonical_sha256(
            [row["destination"] for row in manifest_rows]
        ),
        "import_build_index": immutable_evidence.artifact_ref(index_path),
    }
    manifest_digest = canonical_sha256(manifest)
    manifest_path = (
        destination_raw / "compatibility-imports" / manifest_digest / "manifest.json"
    )
    persist_record(manifest_path, manifest)
    campaign.write_json(
        destination_raw / "latest-compatibility-import.json",
        {
            "import_id": import_id,
            "manifest": immutable_evidence.artifact_ref(manifest_path),
            "manifest_payload_sha256": manifest_digest,
        },
    )
    return manifest_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-raw", type=pathlib.Path, required=True)
    parser.add_argument("--destination-raw", type=pathlib.Path, required=True)
    parser.add_argument(
        "--suite",
        type=pathlib.Path,
        default=type_isolation_suite_contract.CURRENT_SUITE_PATH,
    )
    parser.add_argument("--toolchain", default=campaign.DEFAULT_TOOLCHAIN)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = run_import(
            source_raw=args.source_raw,
            destination_raw=args.destination_raw,
            suite_path=args.suite,
            toolchain=args.toolchain,
        )
    except (ImportError, campaign.CampaignError) as error:
        raise SystemExit(f"error: {error}") from error
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
