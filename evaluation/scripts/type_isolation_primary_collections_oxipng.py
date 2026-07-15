#!/usr/bin/env python3
"""Run the pinned Collections and Oxipng Type Isolation primary campaigns.

The runner derives build trees from exact upstream commits, builds UniAlloc,
typed-control, and Type Isolation variants, retains compiler audits and raw
process records, and writes one assembler-compatible result per target.  It
keeps failed eligibility gates visible instead of promoting partial evidence.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluation.scripts import realworld_type_isolation_matrix as matrix  # noqa: E402


SUITE_PATH = ROOT / "evaluation/config/type_isolation_primary_suite.json"
_SUITE_MANIFEST = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
SUITE_IMPLEMENTATION_REVISION = str(_SUITE_MANIFEST["implementation"]["git_revision"])
SUITE_IMPLEMENTATION_SHA256 = str(_SUITE_MANIFEST["implementation"]["canonical_sha256"])
DEFAULT_RAW_ROOT = ROOT / "evaluation/raw/type-isolation-primary-v1"
DEFAULT_COLLECTIONS_CHECKOUT = (
    ROOT / "evaluation/external/_checkouts/primary-rust-1.97.0"
)
DEFAULT_OXIPNG_CHECKOUT = ROOT / "evaluation/external/_checkouts/primary-oxipng-v10.1.1"
LEGACY_OXIPNG_CHECKOUT = ROOT / "evaluation/external/_checkouts/realworld-oxipng"

VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
REQUIRED_GATES = (
    "correctness",
    "build_success",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "compiler_route_equivalent",
    "source_audit_retained",
)
CORE_REQUIRED_GATES = (
    "correctness",
    "build_success",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "source_audit_retained",
)
COMPILER_ROUTE_MIN = 0.85
COMPILER_ROUTE_MAX = 1.15
LEGACY_OXIPNG_INPUT_COMMIT = "dea23211ae6259007e068c59ab16929798d00d96"
LEGACY_OXIPNG_INPUT_PATH = "tests/files/issue-141.png"
GNU_TIME = Path("/usr/bin/time")
BENCH_RE = re.compile(
    r"^test\s+(?P<name>\S+)\s+\.\.\.\s+bench:\s+"
    r"(?P<ns>[0-9][0-9,]*(?:\.[0-9]+)?)\s+ns/iter",
    re.MULTILINE,
)
ALLOCATOR_INJECTION = """

// UniAlloc primary-suite allocator injection.
mod unialloc_primary_allocator {
    #[global_allocator]
    static ALLOCATOR: unialloc::UniAlloc = unialloc::UniAlloc;
}
"""


class CampaignError(RuntimeError):
    """Raised when a campaign input, build, or measurement fails closed."""


class HarnessSpec(NamedTuple):
    id: str
    selector: str
    kind: str
    binary: str
    threads: int | None = None


class TargetSpec(NamedTuple):
    id: str
    commit: str
    repository: str
    ref: str
    checkout: Path
    harnesses: tuple[HarnessSpec, ...]


TARGETS = {
    "collections": TargetSpec(
        id="collections",
        commit="2d8144b7880597b6e6d3dfd63a9a9efae3f533d3",
        repository="https://github.com/rust-lang/rust.git",
        ref="1.97.0",
        checkout=DEFAULT_COLLECTIONS_CHECKOUT,
        harnesses=(
            HarnessSpec(
                "binary_heap_push", "binary_heap::bench_push", "libtest", "allocbenches"
            ),
            HarnessSpec(
                "btree_set_clone_remove",
                "btree::set::clone_10k_and_remove_half",
                "libtest",
                "allocbenches",
            ),
            HarnessSpec(
                "slice_random_inserts",
                "slice::random_inserts",
                "libtest",
                "allocbenches",
            ),
            HarnessSpec(
                "vec_in_place_collect_droppable",
                "vec::bench_in_place_collect_droppable",
                "libtest",
                "allocbenches",
            ),
            HarnessSpec(
                "vec_deque_grow_1025",
                "vec_deque::bench_grow_1025",
                "libtest",
                "allocbenches",
            ),
        ),
    ),
    "oxipng": TargetSpec(
        id="oxipng",
        commit="628e241e23f368097883807fa6e985ccf7c00357",
        repository="https://github.com/shssoichiro/oxipng.git",
        ref="v10.1.1",
        checkout=DEFAULT_OXIPNG_CHECKOUT,
        harnesses=(
            HarnessSpec(
                "cli_issue_141_t1",
                "issue-141.png, opt=2, threads=1",
                "cli",
                "oxipng",
                1,
            ),
            HarnessSpec(
                "cli_issue_141_t4",
                "issue-141.png, opt=2, threads=4",
                "cli",
                "oxipng",
                4,
            ),
            HarnessSpec(
                "filters_8bit_filter_0",
                "filters_8_bits_filter_0",
                "libtest",
                "filters",
            ),
            HarnessSpec("filters_entropy", "filters_entropy", "libtest", "strategies"),
            HarnessSpec(
                "reductions_rgba_to_palette_8",
                "reductions_rgba_to_palette_8",
                "libtest",
                "reductions",
            ),
        ),
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "target" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def persist_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def command_text(
    command: Sequence[str | os.PathLike[str]], *, cwd: Path, timeout: int = 300
) -> str:
    result = subprocess.run(
        [str(value) for value in command],
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise CampaignError(
            f"command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            + result.stderr[-8000:]
        )
    return result.stdout.strip()


def verify_suite_contract() -> None:
    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    indexed = {target["id"]: target for target in suite["targets"]}
    bounds = suite.get("compiler_route_equivalence_gate")
    if not isinstance(bounds, dict):
        raise CampaignError("suite compiler-route equivalence contract is missing")
    observed_bounds = (
        float(bounds.get("minimum_ratio", math.nan)),
        float(bounds.get("maximum_ratio", math.nan)),
    )
    if observed_bounds != (COMPILER_ROUTE_MIN, COMPILER_ROUTE_MAX):
        raise CampaignError(f"suite compiler-route bounds changed: {observed_bounds!r}")
    for target_id, spec in TARGETS.items():
        suite_target = indexed.get(target_id)
        if not isinstance(suite_target, dict):
            raise CampaignError(f"suite target is missing: {target_id}")
        if suite_target["source"]["commit"] != spec.commit:
            raise CampaignError(f"suite source commit changed for {target_id}")
        manifest_harnesses = [
            (row["id"], row["selector"]) for row in suite_target["harnesses"]
        ]
        runner_harnesses = [(row.id, row.selector) for row in spec.harnesses]
        if manifest_harnesses != runner_harnesses:
            raise CampaignError(f"suite harness contract changed for {target_id}")


def validate_primary_implementation(revision: object, digest: object) -> None:
    if revision != SUITE_IMPLEMENTATION_REVISION:
        raise CampaignError(
            "primary campaign requires the predeclared implementation revision "
            f"{SUITE_IMPLEMENTATION_REVISION}; observed {revision}"
        )
    if digest != SUITE_IMPLEMENTATION_SHA256:
        raise CampaignError(
            "primary campaign requires the predeclared implementation digest "
            f"{SUITE_IMPLEMENTATION_SHA256}; observed {digest}"
        )


def ensure_checkout(spec: TargetSpec) -> dict[str, str]:
    checkout = spec.checkout
    if not (checkout / ".git").exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        checkout.mkdir()
        command_text(["git", "init"], cwd=checkout)
        command_text(["git", "remote", "add", "origin", spec.repository], cwd=checkout)
        if spec.id == "collections":
            command_text(["git", "sparse-checkout", "init", "--cone"], cwd=checkout)
            command_text(
                ["git", "sparse-checkout", "set", "library/alloctests"], cwd=checkout
            )
        command_text(
            [
                "git",
                "fetch",
                "--depth=1",
                "--filter=blob:none",
                "origin",
                f"refs/tags/{spec.ref}",
            ],
            cwd=checkout,
            timeout=1800,
        )
        command_text(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=checkout)
    head = command_text(["git", "rev-parse", "HEAD"], cwd=checkout)
    status = command_text(["git", "status", "--short"], cwd=checkout)
    if head != spec.commit:
        raise CampaignError(
            f"{spec.id} checkout HEAD mismatch: got {head}, expected {spec.commit}"
        )
    if status:
        raise CampaignError(f"{spec.id} checkout is dirty:\n{status}")
    return {
        "path": str(checkout.resolve()),
        "head": head,
        "status": status,
        "remote": command_text(["git", "remote", "get-url", "origin"], cwd=checkout),
    }


def materialize_oxipng_input(raw_root: Path) -> dict[str, Any]:
    if not (LEGACY_OXIPNG_CHECKOUT / ".git").exists():
        raise CampaignError(
            f"legacy Oxipng fixture checkout is missing: {LEGACY_OXIPNG_CHECKOUT}"
        )
    exists = subprocess.run(
        [
            "git",
            "cat-file",
            "-e",
            f"{LEGACY_OXIPNG_INPUT_COMMIT}:{LEGACY_OXIPNG_INPUT_PATH}",
        ],
        cwd=LEGACY_OXIPNG_CHECKOUT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if exists.returncode != 0:
        raise CampaignError("the pinned Oxipng issue-141 fixture is unavailable")
    content = subprocess.run(
        [
            "git",
            "show",
            f"{LEGACY_OXIPNG_INPUT_COMMIT}:{LEGACY_OXIPNG_INPUT_PATH}",
        ],
        cwd=LEGACY_OXIPNG_CHECKOUT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    path = raw_root / "inputs/oxipng/issue-141.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "origin_repository": TARGETS["oxipng"].repository,
        "origin_commit": LEGACY_OXIPNG_INPUT_COMMIT,
        "origin_path": LEGACY_OXIPNG_INPUT_PATH,
    }


def write_source_audit(spec: TargetSpec, raw_root: Path) -> tuple[Path, dict[str, Any]]:
    checkout = ensure_checkout(spec)
    source_root = (
        spec.checkout / "library/alloctests"
        if spec.id == "collections"
        else spec.checkout
    )
    if not (source_root / "Cargo.toml").is_file():
        raise CampaignError(
            f"source Cargo.toml is missing for {spec.id}: {source_root}"
        )
    audit: dict[str, Any] = {
        "schema_version": 1,
        "target_id": spec.id,
        "expected_ref": spec.ref,
        "expected_commit": spec.commit,
        "checkout": checkout,
        "source_root": str(source_root.resolve()),
        "source_tree_sha256": sha256_tree(source_root),
        "cargo_toml_sha256": sha256_file(source_root / "Cargo.toml"),
    }
    if (source_root / "Cargo.lock").is_file():
        audit["upstream_cargo_lock_sha256"] = sha256_file(source_root / "Cargo.lock")
    if spec.id == "oxipng":
        audit["input"] = materialize_oxipng_input(raw_root)
    path = raw_root / f"sources/{spec.id}/source-audit.json"
    persist_json(path, audit)
    return path, audit


def implementation_digest(snapshot: Path) -> str:
    digest = hashlib.sha256()
    roots = (
        snapshot / "unialloc",
        snapshot / "alloc_macros",
    )
    files = [
        snapshot / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
    ]
    for source_root in roots:
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and "target" not in path.parts
            and (path.suffix in {".rs", ".toml"} or path.name == "build.rs")
        )
    for path in sorted(set(files)):
        digest.update(path.relative_to(snapshot).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def campaign_snapshot_digest(snapshot: Path) -> tuple[str, int, int]:
    files = [snapshot / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain")]
    files.extend(
        [snapshot / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"]
    )
    for source_root in (snapshot / "unialloc", snapshot / "alloc_macros"):
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and "target" not in path.parts
            and ".git" not in path.parts
        )
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise CampaignError(
            "campaign snapshot is missing required files: "
            + ",".join(str(path) for path in missing)
        )
    digest = hashlib.sha256()
    size_bytes = 0
    selected = sorted(set(files))
    for path in selected:
        payload = path.read_bytes()
        digest.update(path.relative_to(snapshot).as_posix().encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        size_bytes += len(payload)
    return digest.hexdigest(), len(selected), size_bytes


def current_working_tree_source_identity() -> dict[str, Any]:
    repository_head = command_text(["git", "rev-parse", "HEAD"], cwd=ROOT)
    repository_status = command_text(["git", "status", "--short"], cwd=ROOT)
    context_digest, context_count, context_size = campaign_snapshot_digest(ROOT)
    return {
        "source_kind": "working_tree",
        "campaign_classification": "diagnostic_current_worktree",
        "primary_eligible": False,
        "repository_head": repository_head,
        "repository_status": repository_status,
        "repository_status_sha256": hashlib.sha256(
            repository_status.encode()
        ).hexdigest(),
        "implementation_revision": repository_head,
        "unialloc_implementation_sha256": implementation_digest(ROOT),
        "campaign_snapshot_sha256": context_digest,
        "campaign_snapshot_file_count": context_count,
        "campaign_snapshot_size_bytes": context_size,
    }


def validate_diagnostic_measure_source(source: dict[str, Any] | None) -> None:
    if (
        not isinstance(source, dict)
        or source.get("source_kind") != "working_tree"
        or source.get("primary_eligible") is not False
    ):
        raise CampaignError("diagnostic preflight lacks a working-tree source record")
    live = current_working_tree_source_identity()
    if source.get("repository_head") != live["repository_head"]:
        raise CampaignError("working-tree repository head changed after preflight")
    if source.get("repository_status") != live["repository_status"]:
        raise CampaignError("working-tree repository status changed after preflight")
    if (
        source.get("unialloc_implementation_sha256")
        != live["unialloc_implementation_sha256"]
    ):
        raise CampaignError(
            "working-tree implementation digest changed after preflight"
        )
    context_fields = (
        "campaign_snapshot_sha256",
        "campaign_snapshot_file_count",
        "campaign_snapshot_size_bytes",
    )
    if any(source.get(field) != live[field] for field in context_fields):
        raise CampaignError("working-tree context digest changed after preflight")


def resolve_implementation_revision(revision: str) -> str:
    resolved = command_text(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=ROOT
    )
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise CampaignError(f"implementation revision is invalid: {resolved!r}")
    return resolved


def materialize_git_implementation(revision: str, destination: Path) -> None:
    destination.mkdir(parents=True)
    archive = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            revision,
            "--",
            "unialloc",
            "alloc_macros",
            "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs",
        ],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if archive.returncode != 0:
        raise CampaignError(
            f"implementation archive failed ({archive.returncode}): "
            + archive.stderr.decode("utf-8", errors="replace")[-8000:]
        )
    extracted = subprocess.run(
        ["tar", "-xf", "-", "-C", str(destination)],
        check=False,
        input=archive.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if extracted.returncode != 0:
        raise CampaignError(
            f"implementation archive extraction failed ({extracted.returncode}): "
            + extracted.stderr.decode("utf-8", errors="replace")[-8000:]
        )


def freeze_unialloc_implementation(
    raw_root: Path, implementation_revision: str | None = None
) -> tuple[Path, str]:
    parent = raw_root / "frozen-implementation"
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f"snapshot-{os.getpid()}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    if implementation_revision is None:
        repository_head = command_text(["git", "rev-parse", "HEAD"], cwd=ROOT)
        repository_status = command_text(["git", "status", "--short"], cwd=ROOT)
        source_digest = implementation_digest(ROOT)
        source_context = campaign_snapshot_digest(ROOT)
        (staging / "tools/unialloc-rustc-pass").mkdir(parents=True)
        for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain"):
            shutil.copy2(ROOT / name, staging / name)
        shutil.copytree(
            ROOT / "unialloc",
            staging / "unialloc",
            ignore=shutil.ignore_patterns("target", ".git"),
        )
        shutil.copytree(
            ROOT / "alloc_macros",
            staging / "alloc_macros",
            ignore=shutil.ignore_patterns("target", ".git"),
        )
        shutil.copy2(
            matrix.PASS_SOURCE,
            staging / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs",
        )
        source_kind = "working_tree"
        resolved_revision = repository_head
        if (
            command_text(["git", "rev-parse", "HEAD"], cwd=ROOT) != repository_head
            or command_text(["git", "status", "--short"], cwd=ROOT) != repository_status
            or implementation_digest(ROOT) != source_digest
            or campaign_snapshot_digest(ROOT) != source_context
        ):
            shutil.rmtree(staging, ignore_errors=True)
            raise CampaignError("working tree changed while it was being frozen")
    else:
        resolved_revision = resolve_implementation_revision(implementation_revision)
        materialize_git_implementation(resolved_revision, staging)
        source_kind = "git_revision"
    digest = implementation_digest(staging)
    if implementation_revision is None:
        context_digest, context_count, context_size = campaign_snapshot_digest(staging)
        if (
            digest != source_digest
            or (
                context_digest,
                context_count,
                context_size,
            )
            != source_context
        ):
            shutil.rmtree(staging, ignore_errors=True)
            raise CampaignError("frozen working-tree snapshot differs from its source")
        status_digest = hashlib.sha256(repository_status.encode()).hexdigest()
        destination = parent / (
            f"working-tree-{digest}-{context_digest[:16]}-{status_digest[:16]}"
        )
    else:
        context_digest = None
        context_count = None
        context_size = None
        repository_head = command_text(["git", "rev-parse", "HEAD"], cwd=ROOT)
        repository_status = command_text(["git", "status", "--short"], cwd=ROOT)
        status_digest = hashlib.sha256(repository_status.encode()).hexdigest()
        destination = parent / digest
    if destination.exists():
        if implementation_digest(destination) != digest:
            raise CampaignError(
                f"corrupt frozen implementation snapshot: {destination}"
            )
        if (
            implementation_revision is None
            and campaign_snapshot_digest(destination) != source_context
        ):
            raise CampaignError(
                f"corrupt frozen working-tree context snapshot: {destination}"
            )
        shutil.rmtree(staging)
    else:
        staging.replace(destination)
    audit = {
        "schema_version": 1,
        "unialloc_implementation_sha256": digest,
        "snapshot": str(destination.resolve()),
        "repository_head": command_text(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "repository_status": command_text(["git", "status", "--short"], cwd=ROOT),
        "source_kind": source_kind,
        "implementation_revision": resolved_revision,
        "unialloc_tree_sha256": sha256_tree(destination / "unialloc"),
        "alloc_macros_tree_sha256": sha256_tree(destination / "alloc_macros"),
        "pass_source_sha256": sha256_file(
            destination
            / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
        ),
    }
    if implementation_revision is None:
        audit.update(
            {
                "campaign_classification": "diagnostic_current_worktree",
                "primary_eligible": False,
                "campaign_snapshot_sha256": context_digest,
                "campaign_snapshot_file_count": context_count,
                "campaign_snapshot_size_bytes": context_size,
                "repository_head": repository_head,
                "repository_status": repository_status,
                "repository_status_sha256": status_digest,
            }
        )
    persist_json(destination / "snapshot.json", audit)
    return destination, digest


def ensure_frozen_wrapper(
    snapshot: Path,
    digest: str,
    *,
    raw_root: Path,
    toolchain: str,
    timeout: int,
) -> Path:
    wrapper_dir = raw_root / f"tools/{digest}"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / "unialloc-rustc-wrapper"
    record_path = wrapper_dir / "build.json"
    pass_source = (
        snapshot / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
    )
    if wrapper.is_file() and record_path.is_file():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if (
            record.get("unialloc_implementation_sha256") == digest
            and record.get("toolchain") == toolchain
            and record.get("wrapper_sha256") == sha256_file(wrapper)
        ):
            return wrapper.resolve()
    temporary = wrapper.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    result = matrix.execute(
        [
            "rustc",
            f"+{toolchain}",
            "--cfg",
            "unialloc_rustc_current",
            pass_source,
            "-O",
            "-o",
            temporary,
        ],
        cwd=ROOT,
        env=env,
        timeout=timeout,
    )
    if result["exit_code"] != 0 or result["timed_out"] or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise CampaignError(
            "frozen rustc wrapper build failed:\n"
            + result["stderr"].decode("utf-8", errors="replace")[-8000:]
        )
    temporary.replace(wrapper)
    persist_json(
        record_path,
        {
            "schema_version": 1,
            "success": True,
            "toolchain": toolchain,
            "unialloc_implementation_sha256": digest,
            "pass_source": str(pass_source.resolve()),
            "pass_source_sha256": sha256_file(pass_source),
            "wrapper_sha256": sha256_file(wrapper),
            "command": result["command"],
        },
    )
    return wrapper.resolve()


def copy_source(spec: TargetSpec, destination: Path) -> None:
    source = (
        spec.checkout / "library/alloctests"
        if spec.id == "collections"
        else spec.checkout
    )
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source, destination, ignore=shutil.ignore_patterns(".git", "target")
    )


def append_allocator(source: Path) -> None:
    text = source.read_text(encoding="utf-8")
    marker = "// UniAlloc primary-suite allocator injection."
    if marker in text:
        raise CampaignError(f"allocator injection already exists: {source}")
    source.write_text(text.rstrip() + ALLOCATOR_INJECTION, encoding="utf-8")


def patch_build_tree(
    spec: TargetSpec, variant: str, worktree: Path, implementation: Path
) -> dict[str, Any]:
    manifest = worktree / "Cargo.toml"
    matrix.ensure_standalone_workspace(manifest)
    features = () if variant == "unialloc" else ("type_isolation",)
    dependency = matrix.cargo_path_dependency(implementation / "unialloc", features)
    matrix.add_dependency(manifest, dependency)
    matrix.add_spin_patch(manifest, matrix.find_cached_spin())
    injected: list[Path] = []
    if spec.id == "collections":
        injected = [worktree / "benches/lib.rs"]
    else:
        matrix.force_load_unialloc(manifest)
        injected = [
            worktree / "src/main.rs",
            worktree / "benches/filters.rs",
            worktree / "benches/strategies.rs",
            worktree / "benches/reductions.rs",
        ]
    for source in injected:
        append_allocator(source)
    return {
        "manifest": str(manifest.resolve()),
        "manifest_sha256": sha256_file(manifest),
        "features": list(features),
        "default_features_enabled": True,
        "injected_sources": [str(path.resolve()) for path in injected],
        "injected_source_sha256": {
            str(path.name): sha256_file(path) for path in injected
        },
    }


def build_commands(spec: TargetSpec, toolchain: str, jobs: int) -> list[list[str]]:
    cargo = ["cargo", f"+{toolchain}"]
    if spec.id == "collections":
        return [
            [
                *cargo,
                "bench",
                "--no-run",
                "--locked",
                "--bench",
                "allocbenches",
                "--jobs",
                str(jobs),
            ]
        ]
    return [
        [
            *cargo,
            "build",
            "--release",
            "--locked",
            "--bin",
            "oxipng",
            "--jobs",
            str(jobs),
        ],
        [
            *cargo,
            "bench",
            "--no-run",
            "--locked",
            "--bench",
            "filters",
            "--bench",
            "strategies",
            "--bench",
            "reductions",
            "--jobs",
            str(jobs),
        ],
    ]


def find_executable(directory: Path, stem: str) -> Path:
    if stem == "oxipng" and (directory / "release/oxipng").is_file():
        return (directory / "release/oxipng").resolve()
    candidates = [
        path
        for path in (directory / "release/deps").glob(f"{stem}-*")
        if path.is_file() and os.access(path, os.X_OK) and path.suffix != ".d"
    ]
    if len(candidates) != 1:
        raise CampaignError(f"expected one {stem} executable, found {candidates}")
    return candidates[0].resolve()


def allocator_activation_proof(binaries: dict[str, Path], out: Path) -> dict[str, Any]:
    details: dict[str, Any] = {}
    success = True
    for name, binary in binaries.items():
        result = subprocess.run(
            ["nm", "-C", "--defined-only", str(binary)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        symbols = result.stdout.decode("utf-8", errors="replace")
        matches = sorted(
            {
                line.strip()
                for line in symbols.splitlines()
                if "unialloc::" in line or "__unialloc_" in line
            }
        )
        record = {
            "binary": str(binary),
            "binary_sha256": sha256_file(binary),
            "nm_exit_code": result.returncode,
            "nm_stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "unialloc_symbol_count": len(matches),
            "sample_symbols": matches[:32],
            "success": result.returncode == 0 and bool(matches),
        }
        details[name] = record
        success = success and bool(record["success"])
    proof = {"success": success, "binaries": details}
    persist_json(out, proof)
    return proof


def target_crates(spec: TargetSpec) -> tuple[str, ...]:
    if spec.id == "collections":
        return ("allocbenches",)
    return ("oxipng", "filters", "strategies", "reductions")


def build_variant(
    spec: TargetSpec,
    variant: str,
    *,
    raw_root: Path,
    implementation: Path,
    unialloc_implementation_sha256: str,
    toolchain: str,
    jobs: int,
    build_timeout: int,
    wrapper: Path,
    sysroot: Path,
) -> dict[str, Any]:
    build_root = raw_root / f"builds/{spec.id}/{variant}"
    worktree = build_root / "source"
    target_dir = build_root / "target"
    audit_dir = build_root / "audits"
    pass_log_dir = build_root / "pass-logs"
    copy_source(spec, worktree)
    if implementation_digest(implementation) != unialloc_implementation_sha256:
        raise CampaignError("frozen UniAlloc implementation digest changed")
    patch = patch_build_tree(spec, variant, worktree, implementation)
    target_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    pass_log_dir.mkdir(parents=True, exist_ok=True)
    temporary = raw_root / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    base_env = os.environ.copy()
    base_env.update(
        {
            "CARGO_TARGET_DIR": str(target_dir.resolve()),
            "CARGO_INCREMENTAL": "0",
            "CARGO_PROFILE_RELEASE_STRIP": "none",
            "TMPDIR": str(temporary.resolve()),
        }
    )
    metadata = matrix.execute(
        ["cargo", f"+{toolchain}", "metadata", "--format-version", "1"],
        cwd=worktree,
        env=base_env,
        timeout=build_timeout,
    )
    (build_root / "metadata.stdout").write_bytes(metadata["stdout"])
    (build_root / "metadata.stderr").write_bytes(metadata["stderr"])
    if metadata["exit_code"] != 0 or metadata["timed_out"]:
        raise CampaignError(
            f"Cargo metadata failed for {spec.id}/{variant}:\n"
            + metadata["stderr"].decode("utf-8", errors="replace")[-8000:]
        )
    lock = worktree / "Cargo.lock"
    if not lock.is_file():
        raise CampaignError(f"derived Cargo.lock is missing for {spec.id}/{variant}")
    env = dict(base_env)
    if variant in {"typed_plain", "typeiso_perf"}:
        env = matrix.typeiso_environment(
            env,
            wrapper=wrapper,
            audit_dir=audit_dir,
            pass_log_dir=pass_log_dir,
            sysroot=sysroot,
            target_crates=target_crates(spec),
            policy_flags=0 if variant == "typed_plain" else 1,
        )
    commands: list[dict[str, Any]] = []
    for index, command in enumerate(build_commands(spec, toolchain, jobs)):
        result = matrix.execute(command, cwd=worktree, env=env, timeout=build_timeout)
        (build_root / f"build-{index}.stdout").write_bytes(result["stdout"])
        (build_root / f"build-{index}.stderr").write_bytes(result["stderr"])
        commands.append(
            {
                "command": result["command"],
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
                "wall_seconds": result["wall_seconds"],
                "stdout_sha256": hashlib.sha256(result["stdout"]).hexdigest(),
                "stderr_sha256": hashlib.sha256(result["stderr"]).hexdigest(),
            }
        )
        if result["exit_code"] != 0 or result["timed_out"]:
            raise CampaignError(
                f"build failed for {spec.id}/{variant}:\n"
                + result["stderr"].decode("utf-8", errors="replace")[-8000:]
            )
    names = sorted({harness.binary for harness in spec.harnesses})
    binaries = {name: find_executable(target_dir, name) for name in names}
    activation = allocator_activation_proof(
        binaries, build_root / "allocator-activation.json"
    )
    audit = None
    actual_mir = variant == "unialloc"
    if variant in {"typed_plain", "typeiso_perf"}:
        audit = matrix.summarize_audits(audit_dir)
        try:
            matrix.validate_typeiso_audits(audit, target_crates(spec))
        except matrix.MatrixError as error:
            raise CampaignError(str(error)) from error
        actual_mir = True
    record = {
        "schema_version": 1,
        "target_id": spec.id,
        "variant": variant,
        "source_commit": spec.commit,
        "unialloc_implementation_sha256": unialloc_implementation_sha256,
        "unialloc_implementation_snapshot": str(implementation.resolve()),
        "toolchain": toolchain,
        "patch": patch,
        "derived_source_sha256": sha256_tree(worktree),
        "derived_cargo_lock": str(lock.resolve()),
        "derived_cargo_lock_sha256": sha256_file(lock),
        "stats_feature_enabled": False,
        "policy_flags": (
            0
            if variant == "typed_plain"
            else (1 if variant == "typeiso_perf" else None)
        ),
        "commands": commands,
        "binaries": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in binaries.items()
        },
        "allocator_activation": activation,
        "actual_mir_provenance": actual_mir,
        "audit": audit,
        "audit_dir": str(audit_dir.resolve()),
        "pass_log_dir": str(pass_log_dir.resolve()),
        "success": bool(activation["success"] and actual_mir),
    }
    persist_json(build_root / "build.json", record)
    return record


def parse_libtest_benchmark(stdout: str, expected: str) -> float:
    matches = list(BENCH_RE.finditer(stdout))
    if len(matches) != 1 or matches[0].group("name") != expected:
        observed = [match.group("name") for match in matches]
        raise CampaignError(
            f"expected one exact benchmark {expected!r}, observed {observed!r}"
        )
    return float(matches[0].group("ns").replace(",", ""))


def clean_runtime_environment(raw_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("UNIALLOC_") or key in {
            "GLIBC_TUNABLES",
            "LD_PRELOAD",
            "MALLOC_CONF",
            "MALLOC_ARENA_MAX",
        }:
            env.pop(key, None)
    env["TMPDIR"] = str((raw_root / "tmp").resolve())
    return env


@contextmanager
def primary_measurement_lock(raw_root: Path):
    lock_path = DEFAULT_RAW_ROOT / "primary-measurement.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "runner": Path(__file__).name,
                    "raw_root": str(raw_root.resolve()),
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()
        try:
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def rotated_variants(round_index: int, harness_index: int) -> tuple[str, ...]:
    offset = (round_index + harness_index) % len(VARIANTS)
    return VARIANTS[offset:] + VARIANTS[:offset]


def run_harness(
    spec: TargetSpec,
    harness: HarnessSpec,
    variant: str,
    *,
    phase: str,
    round_number: int,
    raw_root: Path,
    build: dict[str, Any],
    command_prefix: Sequence[str],
    timeout: int,
    oxipng_input: Path | None,
) -> dict[str, Any]:
    run_dir = (
        raw_root
        / f"runs/{spec.id}/{harness.id}/{phase}/round-{round_number:02d}/{variant}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    binary = Path(build["binaries"][harness.binary]["path"])
    output_file: Path | None = None
    if harness.kind == "libtest":
        command = [
            str(binary),
            "--bench",
            "--exact",
            harness.selector,
            "--test-threads=1",
        ]
        cwd = Path(build["patch"]["manifest"]).parent
    else:
        if oxipng_input is None or harness.threads is None:
            raise CampaignError("Oxipng CLI input or thread count is missing")
        output_file = run_dir / "output.png"
        command = [
            str(binary),
            "--opt",
            "2",
            "--threads",
            str(harness.threads),
            "--force",
            "--quiet",
            "--out",
            str(output_file),
            str(oxipng_input),
        ]
        cwd = run_dir
    load_before = Path("/proc/loadavg").read_text(encoding="utf-8").strip()
    measured = matrix.run_measured(
        command,
        cwd=cwd,
        env=clean_runtime_environment(raw_root),
        time_binary=GNU_TIME,
        rss_path=run_dir / "gnu-time.txt",
        timeout=timeout,
        command_prefix=command_prefix,
    )
    load_after = Path("/proc/loadavg").read_text(encoding="utf-8").strip()
    stdout = measured.pop("stdout")
    stderr = measured.pop("stderr")
    (run_dir / "stdout.bin").write_bytes(stdout)
    (run_dir / "stderr.bin").write_bytes(stderr)
    if measured["exit_code"] != 0 or measured["timed_out"]:
        raise CampaignError(
            f"workload failed for {spec.id}/{harness.id}/{variant}: {measured}"
        )
    if measured["gnu_time_exit_status"] != 0 or measured["peak_rss_kib"] <= 0:
        raise CampaignError(
            f"invalid GNU time result for {spec.id}/{harness.id}/{variant}"
        )
    if harness.kind == "libtest":
        performance = parse_libtest_benchmark(
            stdout.decode("utf-8", errors="replace"), harness.selector
        )
        correctness = {
            "oracle": "exact libtest benchmark completed",
            "reported_benchmark": harness.selector,
        }
    else:
        if output_file is None or not output_file.is_file():
            raise CampaignError(f"Oxipng produced no output for {harness.id}/{variant}")
        output = output_file.read_bytes()
        if not output.startswith(b"\x89PNG\r\n\x1a\n"):
            raise CampaignError(
                f"Oxipng output is not a PNG for {harness.id}/{variant}"
            )
        performance = float(measured["wall_seconds"])
        correctness = {
            "oracle": "successful PNG output with stable content digest",
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_size_bytes": len(output),
        }
    record = {
        "schema_version": 1,
        "target_id": spec.id,
        "source_commit": spec.commit,
        "harness_id": harness.id,
        "selector": harness.selector,
        "phase": phase,
        "round": round_number,
        "variant": variant,
        "performance": performance,
        "performance_unit": "ns_per_iter" if harness.kind == "libtest" else "seconds",
        "peak_rss_mib": float(measured["peak_rss_kib"]) / 1024.0,
        "correctness": correctness,
        "stats_disabled": True,
        "glibc_rseq_mode": "libc_default",
        "affinity": {
            "command_prefix": list(command_prefix),
            "expected_cpu_list": "0-15",
        },
        "host_load": {"before": load_before, "after": load_after},
        "measurement": measured,
        "stdout_path": str((run_dir / "stdout.bin").resolve()),
        "stderr_path": str((run_dir / "stderr.bin").resolve()),
        "gnu_time_path": str((run_dir / "gnu-time.txt").resolve()),
    }
    persist_json(run_dir / "record.json", record)
    return record


def validate_cli_output_digests(records: Sequence[dict[str, Any]]) -> None:
    digests = {record["correctness"]["output_sha256"] for record in records}
    if len(digests) != 1:
        raise CampaignError(f"Oxipng output digest mismatch: {sorted(digests)}")


def compiler_route_equivalence(
    measurements: Sequence[dict[str, Any]],
) -> tuple[bool, float]:
    index = {
        (int(row["round"]), str(row["variant"])): float(row["performance"])
        for row in measurements
    }
    rounds = sorted({int(row["round"]) for row in measurements})
    ratios = [
        index[(round_number, "typed_plain")] / index[(round_number, "unialloc")]
        for round_number in rounds
    ]
    median_ratio = statistics.median(ratios)
    return (
        COMPILER_ROUTE_MIN <= median_ratio <= COMPILER_ROUTE_MAX,
        median_ratio,
    )


def validate_measurements(
    spec: TargetSpec, measurements: dict[str, list[dict[str, Any]]]
) -> None:
    expected = {
        (round_number, variant) for round_number in range(1, 6) for variant in VARIANTS
    }
    if set(measurements) != {harness.id for harness in spec.harnesses}:
        raise CampaignError(f"measurement harness mismatch for {spec.id}")
    for harness in spec.harnesses:
        rows = measurements[harness.id]
        observed = {(int(row["round"]), str(row["variant"])) for row in rows}
        if observed != expected or len(rows) != len(expected):
            raise CampaignError(
                f"{spec.id}/{harness.id} does not have five complete paired rounds"
            )
        for row in rows:
            for field in ("performance", "peak_rss_mib"):
                value = float(row[field])
                if not math.isfinite(value) or value <= 0:
                    raise CampaignError(
                        f"{spec.id}/{harness.id} has invalid {field}: {value}"
                    )


def validate_warmup_evidence(
    target_id: str,
    harness_id: str,
    evidence: Any,
) -> None:
    if not isinstance(evidence, dict) or set(evidence) != set(VARIANTS):
        raise CampaignError(
            f"{target_id}/{harness_id} warmup evidence must cover every variant"
        )
    seen_paths: set[Path] = set()
    for variant in VARIANTS:
        entries = evidence[variant]
        if not isinstance(entries, list) or not entries:
            raise CampaignError(
                f"{target_id}/{harness_id}/{variant} needs retained warmup evidence"
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise CampaignError(
                    f"{target_id}/{harness_id}/{variant} warmup entry is invalid"
                )
            path_value = entry.get("record_path")
            digest = entry.get("sha256")
            if not isinstance(path_value, str) or not path_value:
                raise CampaignError(
                    f"{target_id}/{harness_id}/{variant} warmup path is invalid"
                )
            path = Path(path_value)
            if path in seen_paths or not path.is_file():
                raise CampaignError(
                    f"{target_id}/{harness_id}/{variant} warmup record is missing"
                )
            seen_paths.add(path)
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or sha256_file(path) != digest
            ):
                raise CampaignError(
                    f"{target_id}/{harness_id}/{variant} warmup digest mismatch"
                )
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CampaignError(
                    f"{target_id}/{harness_id}/{variant} warmup record is invalid"
                ) from error
            if (
                not isinstance(record, dict)
                or record.get("target_id") != target_id
                or record.get("harness_id") != harness_id
                or record.get("variant") != variant
                or record.get("phase") != "warmup"
                or record.get("round") != 0
            ):
                raise CampaignError(
                    f"{target_id}/{harness_id}/{variant} warmup identity mismatch"
                )
            for field in ("performance", "peak_rss_mib"):
                value = record.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) <= 0.0
                ):
                    raise CampaignError(
                        f"{target_id}/{harness_id}/{variant} warmup {field} is invalid"
                    )


def warmup_evidence_from_paths(
    spec: TargetSpec, paths: Sequence[str]
) -> dict[str, dict[str, list[dict[str, str]]]]:
    evidence = {
        harness.id: {variant: [] for variant in VARIANTS} for harness in spec.harnesses
    }
    for path_value in paths:
        path = Path(path_value)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CampaignError(f"warmup record is unreadable: {path}") from error
        harness_id = record.get("harness_id") if isinstance(record, dict) else None
        variant = record.get("variant") if isinstance(record, dict) else None
        if harness_id not in evidence or variant not in VARIANTS:
            raise CampaignError(f"warmup record identity is unexpected: {path}")
        evidence[str(harness_id)][str(variant)].append(
            {"record_path": str(path.resolve()), "sha256": sha256_file(path)}
        )
    for harness in spec.harnesses:
        validate_warmup_evidence(spec.id, harness.id, evidence[harness.id])
    return evidence


def retained_build_records_succeeded(
    spec: TargetSpec,
    raw_root: Path,
    builds: dict[str, dict[str, Any]],
) -> bool:
    for variant in VARIANTS:
        path = raw_root / f"builds/{spec.id}/{variant}/build.json"
        if not path.is_file() or builds[variant].get("success") is not True:
            return False
        try:
            retained = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if retained != builds[variant] or retained.get("success") is not True:
            return False
    return True


def build_target_result(
    spec: TargetSpec,
    *,
    implementation_revision: str | None,
    implementation_sha256: str,
    warmup_evidence: dict[str, dict[str, list[dict[str, str]]]],
    measurements: dict[str, list[dict[str, Any]]],
    build_gates: dict[str, bool],
    raw_root: Path,
    source_audit_path: Path,
    build_records: dict[str, dict[str, Any]],
    raw_records: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    validate_measurements(spec, measurements)
    harnesses: list[dict[str, Any]] = []
    for harness in spec.harnesses:
        rows = measurements[harness.id]
        harness_warmups = warmup_evidence.get(harness.id)
        validate_warmup_evidence(spec.id, harness.id, harness_warmups)
        route_equivalent, route_ratio = compiler_route_equivalence(rows)
        gates = {
            "correctness": True,
            "build_success": bool(build_gates["build_success"]),
            "allocator_activation": bool(build_gates["allocator_activation"]),
            "actual_mir_provenance": bool(build_gates["actual_mir_provenance"]),
            "stats_disabled": bool(build_gates["stats_disabled"]),
            "compiler_route_equivalent": route_equivalent,
            "source_audit_retained": bool(build_gates["source_audit_retained"]),
        }
        harnesses.append(
            {
                "id": harness.id,
                "metric_direction": "lower_is_better",
                "gates": gates,
                "warmup_evidence": harness_warmups,
                "measurements": rows,
                "evidence": {
                    "selector": harness.selector,
                    "performance_unit": raw_records[harness.id][0]["performance_unit"]
                    if raw_records[harness.id]
                    else None,
                    "compiler_route_cost_ratio_bounds": [
                        COMPILER_ROUTE_MIN,
                        COMPILER_ROUTE_MAX,
                    ],
                    "compiler_route_median_cost_ratio": route_ratio,
                    "raw_record_paths": [
                        record.get("record_path") for record in raw_records[harness.id]
                    ],
                },
            }
        )
    return {
        "schema_version": 1,
        "target_id": spec.id,
        "source_commit": spec.commit,
        "implementation_revision": implementation_revision,
        "implementation_sha256": implementation_sha256,
        "harnesses": harnesses,
        "evidence": {
            "raw_root": str(raw_root.resolve()),
            "source_audit": str(source_audit_path.resolve()),
            "build_records": {
                variant: str(
                    (raw_root / f"builds/{spec.id}/{variant}/build.json").resolve()
                )
                for variant in build_records
            },
        },
    }


def validate_core_result(result: dict[str, Any]) -> None:
    if result.get("schema_version") != 1:
        raise CampaignError("target result schema version must be 1")
    harnesses = result.get("harnesses")
    if not isinstance(harnesses, list) or not harnesses:
        raise CampaignError("target result must contain harnesses")
    for harness in harnesses:
        gates = harness.get("gates")
        if not isinstance(gates, dict):
            raise CampaignError(f"{harness.get('id')} gates are missing")
        failed = [gate for gate in CORE_REQUIRED_GATES if gates.get(gate) is not True]
        if failed:
            raise CampaignError(
                f"{harness.get('id')} failed core gates: {','.join(failed)}"
            )
        validate_warmup_evidence(
            str(result.get("target_id")),
            str(harness.get("id")),
            harness.get("warmup_evidence"),
        )


def publish_target_results(
    result_paths: Sequence[Path],
    *,
    destination: Path = DEFAULT_RAW_ROOT / "targets",
) -> tuple[Path, ...]:
    records: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for result_path in result_paths:
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CampaignError(
                f"target result is unreadable: {result_path}"
            ) from error
        if not isinstance(result, dict):
            raise CampaignError(f"target result must be an object: {result_path}")
        if (
            result.get("campaign_classification") == "diagnostic_current_worktree"
            or result.get("primary_eligible") is False
        ):
            raise CampaignError(
                f"diagnostic result cannot enter primary publication: {result_path}"
            )
        validate_primary_implementation(
            result.get("implementation_revision"),
            result.get("implementation_sha256"),
        )
        validate_core_result(result)
        target_id = result.get("target_id")
        if (
            not isinstance(target_id, str)
            or target_id not in TARGETS
            or target_id in seen
        ):
            raise CampaignError(f"target result identity is invalid: {target_id!r}")
        seen.add(target_id)
        records.append((target_id, result))

    destination.mkdir(parents=True, exist_ok=True)
    published: list[Path] = []
    for target_id, result in records:
        output = destination / f"{target_id}.json"
        persist_json(output, result)
        published.append(output)
    return tuple(published)


def load_preflight(
    spec: TargetSpec,
    raw_root: Path,
    expected_implementation_revision: str | None,
) -> tuple[
    Path,
    dict[str, Any],
    dict[str, dict[str, Any]],
    str,
    dict[str, dict[str, list[dict[str, str]]]],
    dict[str, Any] | None,
]:
    preflight_path = raw_root / f"preflight/{spec.id}.json"
    try:
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CampaignError(f"valid preflight is missing for {spec.id}") from error
    if preflight.get("measurement_ready") is not True:
        raise CampaignError(f"preflight is not measurement-ready for {spec.id}")
    if preflight.get("implementation_revision") != expected_implementation_revision:
        raise CampaignError(
            f"preflight implementation revision mismatch for {spec.id}: "
            f"{preflight.get('implementation_revision')!r}"
        )
    digest = str(preflight.get("unialloc_implementation_sha256", ""))
    if len(digest) != 64:
        raise CampaignError(f"preflight implementation digest is invalid for {spec.id}")
    source_audit_path = Path(str(preflight["source_audit"]))
    if not source_audit_path.is_file():
        raise CampaignError(f"source audit is missing for {spec.id}")
    source_audit = json.loads(source_audit_path.read_text(encoding="utf-8"))
    builds: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        path = raw_root / f"builds/{spec.id}/{variant}/build.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            record.get("success") is not True
            or record.get("source_commit") != spec.commit
            or record.get("unialloc_implementation_sha256") != digest
        ):
            raise CampaignError(f"build record is ineligible: {spec.id}/{variant}")
        for binary in record.get("binaries", {}).values():
            binary_path = Path(str(binary["path"]))
            if (
                not binary_path.is_file()
                or sha256_file(binary_path) != binary["sha256"]
            ):
                raise CampaignError(
                    f"build binary digest mismatch: {spec.id}/{variant}/{binary_path.name}"
                )
        builds[variant] = record
    raw_warmup_paths = preflight.get("warmup_record_paths")
    if not isinstance(raw_warmup_paths, list) or not all(
        isinstance(path, str) for path in raw_warmup_paths
    ):
        raise CampaignError(f"preflight warmup evidence is invalid for {spec.id}")
    warmup_evidence = warmup_evidence_from_paths(spec, raw_warmup_paths)
    implementation_source = preflight.get("implementation_source")
    if implementation_source is not None and not isinstance(
        implementation_source, dict
    ):
        raise CampaignError(f"preflight implementation source is invalid for {spec.id}")
    return (
        source_audit_path,
        source_audit,
        builds,
        digest,
        warmup_evidence,
        implementation_source,
    )


def diagnostic_result_metadata(
    implementation_source: dict[str, Any] | None,
    *,
    cpu_list: str,
    numa_node: int,
) -> dict[str, Any]:
    if (
        not isinstance(implementation_source, dict)
        or implementation_source.get("source_kind") != "working_tree"
        or implementation_source.get("primary_eligible") is not False
    ):
        raise CampaignError("diagnostic campaign lacks a working-tree source record")
    return {
        "campaign_classification": "diagnostic_current_worktree",
        "primary_eligible": False,
        "primary_ineligibility_reason": "working_tree_implementation",
        "implementation_source_kind": "working_tree",
        "repository_head": implementation_source["repository_head"],
        "repository_status": implementation_source["repository_status"],
        "repository_status_sha256": implementation_source["repository_status_sha256"],
        "implementation_revision": implementation_source["implementation_revision"],
        "implementation_sha256": implementation_source[
            "unialloc_implementation_sha256"
        ],
        "campaign_snapshot_sha256": implementation_source["campaign_snapshot_sha256"],
        "campaign_snapshot_file_count": implementation_source[
            "campaign_snapshot_file_count"
        ],
        "campaign_snapshot_size_bytes": implementation_source[
            "campaign_snapshot_size_bytes"
        ],
        "measurement_cpu_list": cpu_list,
        "measurement_numa_node": numa_node,
    }


def target_result_path(
    raw_root: Path, target_id: str, *, diagnostic_current_worktree: bool
) -> Path:
    result_directory = (
        "diagnostic-results" if diagnostic_current_worktree else "targets"
    )
    return raw_root / result_directory / f"{target_id}.json"


def run_target(
    spec: TargetSpec,
    *,
    raw_root: Path,
    implementation: Path | None,
    unialloc_implementation_sha256: str | None,
    toolchain: str,
    jobs: int,
    build_timeout: int,
    run_timeout: int,
    cpu_list: str,
    numa_node: int,
    wrapper: Path | None,
    sysroot: Path | None,
    implementation_revision: str | None,
    stage: str,
    diagnostic_current_worktree: bool = False,
    implementation_source: dict[str, Any] | None = None,
) -> Path:
    if stage == "measure":
        (
            source_audit_path,
            source_audit,
            builds,
            preflight_digest,
            warmup_evidence,
            preflight_implementation_source,
        ) = load_preflight(spec, raw_root, implementation_revision)
        unialloc_implementation_sha256 = preflight_digest
        implementation_source = preflight_implementation_source
    else:
        if (
            implementation is None
            or unialloc_implementation_sha256 is None
            or wrapper is None
            or sysroot is None
        ):
            raise CampaignError("preflight build inputs are incomplete")
        source_audit_path, source_audit = write_source_audit(spec, raw_root)
        builds = {
            variant: build_variant(
                spec,
                variant,
                raw_root=raw_root,
                implementation=implementation,
                unialloc_implementation_sha256=unialloc_implementation_sha256,
                toolchain=toolchain,
                jobs=jobs,
                build_timeout=build_timeout,
                wrapper=wrapper,
                sysroot=sysroot,
            )
            for variant in VARIANTS
        }
    if diagnostic_current_worktree:
        if stage == "measure":
            validate_diagnostic_measure_source(implementation_source)
    else:
        if unialloc_implementation_sha256 is None or implementation_revision is None:
            raise CampaignError("primary implementation identity is incomplete")
        validate_primary_implementation(
            implementation_revision, unialloc_implementation_sha256
        )
    typed_source_hashes = {
        builds[variant]["derived_source_sha256"]
        for variant in ("typed_plain", "typeiso_perf")
    }
    policy_only_difference = len(typed_source_hashes) == 1
    implementation_hashes = {
        build["unialloc_implementation_sha256"] for build in builds.values()
    }
    identical_implementation = implementation_hashes == {unialloc_implementation_sha256}
    command_prefix = [
        *matrix.measurement_command_prefix(cpu_list, numa_node),
        "taskset",
        "-c",
        cpu_list,
    ]
    all_records: dict[str, list[dict[str, Any]]] = {
        harness.id: [] for harness in spec.harnesses
    }
    oxipng_input = Path(source_audit["input"]["path"]) if spec.id == "oxipng" else None
    round_indices = range(1, 6) if stage == "measure" else range(6)
    if stage == "preflight":
        round_indices = range(1)
    with primary_measurement_lock(raw_root) as lock_path:
        for round_index in round_indices:
            phase = "warmup" if round_index == 0 else "measured"
            round_number = 0 if round_index == 0 else round_index
            for harness_index, harness in enumerate(spec.harnesses):
                records: list[dict[str, Any]] = []
                for variant in rotated_variants(round_index, harness_index):
                    record = run_harness(
                        spec,
                        harness,
                        variant,
                        phase=phase,
                        round_number=round_number,
                        raw_root=raw_root,
                        build=builds[variant],
                        command_prefix=command_prefix,
                        timeout=run_timeout,
                        oxipng_input=oxipng_input,
                    )
                    record["record_path"] = str(
                        (
                            raw_root
                            / f"runs/{spec.id}/{harness.id}/{phase}/round-{round_number:02d}/{variant}/record.json"
                        ).resolve()
                    )
                    all_records[harness.id].append(record)
                    records.append(record)
                if harness.kind == "cli":
                    validate_cli_output_digests(records)
    if stage == "preflight":
        preflight_path = raw_root / f"preflight/{spec.id}.json"
        persist_json(
            preflight_path,
            {
                "schema_version": 1,
                "target_id": spec.id,
                "source_commit": spec.commit,
                "measurement_ready": bool(
                    identical_implementation
                    and policy_only_difference
                    and all(build["success"] is True for build in builds.values())
                    and all(
                        len(all_records[harness.id]) == len(VARIANTS)
                        for harness in spec.harnesses
                    )
                ),
                "unialloc_implementation_sha256": unialloc_implementation_sha256,
                "implementation_revision": implementation_revision,
                "source_audit": str(source_audit_path.resolve()),
                "build_records": {
                    variant: str(
                        (raw_root / f"builds/{spec.id}/{variant}/build.json").resolve()
                    )
                    for variant in VARIANTS
                },
                "warmup_record_paths": [
                    record["record_path"]
                    for records in all_records.values()
                    for record in records
                ],
                "measurement_lock": str(lock_path.resolve()),
                "measurement_affinity": {
                    "cpu_list": cpu_list,
                    "numa_node": numa_node,
                    "command_prefix": command_prefix,
                },
                **(
                    {
                        "implementation_source": implementation_source,
                        **diagnostic_result_metadata(
                            implementation_source,
                            cpu_list=cpu_list,
                            numa_node=numa_node,
                        ),
                    }
                    if diagnostic_current_worktree
                    else {}
                ),
            },
        )
        return preflight_path
    measured = {
        harness.id: [
            {
                "round": int(record["round"]),
                "variant": str(record["variant"]),
                "performance": float(record["performance"]),
                "peak_rss_mib": float(record["peak_rss_mib"]),
            }
            for record in all_records[harness.id]
            if record["phase"] == "measured"
        ]
        for harness in spec.harnesses
    }
    build_gates = {
        "build_success": retained_build_records_succeeded(spec, raw_root, builds),
        "allocator_activation": all(
            bool(build["allocator_activation"]["success"]) for build in builds.values()
        ),
        "actual_mir_provenance": bool(
            identical_implementation
            and policy_only_difference
            and builds["typed_plain"]["actual_mir_provenance"]
            and builds["typeiso_perf"]["actual_mir_provenance"]
        ),
        "stats_disabled": all(
            build["stats_feature_enabled"] is False for build in builds.values()
        ),
        "source_audit_retained": source_audit_path.is_file(),
    }
    result = build_target_result(
        spec,
        implementation_revision=implementation_revision,
        implementation_sha256=str(unialloc_implementation_sha256),
        warmup_evidence=(
            warmup_evidence
            if stage == "measure"
            else warmup_evidence_from_paths(
                spec,
                [
                    str(record["record_path"])
                    for records in all_records.values()
                    for record in records
                    if record.get("phase") == "warmup"
                ],
            )
        ),
        measurements=measured,
        build_gates=build_gates,
        raw_root=raw_root,
        source_audit_path=source_audit_path,
        build_records=builds,
        raw_records=all_records,
    )
    result_path = target_result_path(
        raw_root,
        spec.id,
        diagnostic_current_worktree=diagnostic_current_worktree,
    )
    persist_json(result_path, result)
    validate_core_result(result)
    limited_harnesses = [
        str(harness["id"])
        for harness in result["harnesses"]
        if harness["gates"].get("compiler_route_equivalent") is not True
    ]
    result["status"] = (
        "complete_with_attribution_limits" if limited_harnesses else "complete"
    )
    result["core_eligible"] = True
    result["attribution_limits"] = [
        {
            "classification": "outside_predeclared_interval",
            "gate": "compiler_route_equivalent",
            "harness_id": harness_id,
        }
        for harness_id in limited_harnesses
    ]
    if diagnostic_current_worktree:
        result.update(
            diagnostic_result_metadata(
                implementation_source,
                cpu_list=cpu_list,
                numa_node=numa_node,
            )
        )
        result["core_eligible"] = False
    persist_json(result_path, result)
    return result_path


def parse_cpu_list(raw: str) -> set[int]:
    cpus: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid CPU range: {token}")
            cpus.update(range(start, end + 1))
        else:
            cpus.add(int(token))
    if not cpus or min(cpus) < 0:
        raise ValueError(f"invalid CPU list: {raw}")
    return cpus


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--targets", default="collections,oxipng", help="comma-separated target ids"
    )
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    implementation = parser.add_mutually_exclusive_group()
    implementation.add_argument(
        "--implementation-revision",
        help="materialize allocator and MIR-pass sources from this Git commit",
    )
    implementation.add_argument(
        "--current-working-tree",
        action="store_true",
        help=(
            "explicitly select the diagnostic-only current working-tree route; "
            "the legacy no-revision route has the same classification"
        ),
    )
    parser.add_argument("--toolchain", default="nightly-2026-06-11")
    parser.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 1))
    parser.add_argument("--build-timeout", type=int, default=1800)
    parser.add_argument("--run-timeout", type=int, default=300)
    parser.add_argument("--cpu-list", default="0-15")
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument(
        "--stage", choices=("preflight", "measure", "all"), default="all"
    )
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    selected = tuple(part.strip() for part in args.targets.split(",") if part.strip())
    unknown = sorted(set(selected) - set(TARGETS))
    if not selected or unknown:
        parser.error(f"invalid targets: {','.join(unknown) if unknown else 'empty'}")
    if (
        args.jobs < 1
        or args.jobs > 16
        or args.build_timeout < 1
        or args.run_timeout < 1
    ):
        parser.error("jobs and timeouts must be positive")
    args.diagnostic_current_worktree = bool(
        args.current_working_tree or args.implementation_revision is None
    )
    if not args.diagnostic_current_worktree and (
        args.cpu_list != "0-15" or args.numa_node != 0
    ):
        parser.error("primary measurements require CPU list 0-15 on NUMA node 0")
    try:
        parse_cpu_list(args.cpu_list)
        if args.numa_node < 0:
            raise ValueError("NUMA node must be non-negative")
    except ValueError as error:
        parser.error(str(error))
    args.targets = selected
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        verify_suite_contract()
        requested_implementation_revision = (
            resolve_implementation_revision(args.implementation_revision)
            if args.implementation_revision is not None
            else None
        )
        if requested_implementation_revision is not None:
            validate_primary_implementation(
                requested_implementation_revision, SUITE_IMPLEMENTATION_SHA256
            )
        implementation_revision = requested_implementation_revision or command_text(
            ["git", "rev-parse", "HEAD"], cwd=ROOT
        )
        if args.describe:
            print(
                json.dumps(
                    {
                        "targets": {
                            target_id: {
                                "commit": TARGETS[target_id].commit,
                                "harnesses": [
                                    harness._asdict()
                                    for harness in TARGETS[target_id].harnesses
                                ],
                            }
                            for target_id in args.targets
                        },
                        "variants": list(VARIANTS),
                        "implementation_revision": implementation_revision,
                        "campaign_classification": (
                            "diagnostic_current_worktree"
                            if args.diagnostic_current_worktree
                            else "pinned_primary"
                        ),
                        "cpu_list": args.cpu_list,
                        "numa_node": args.numa_node,
                        "warmups": 1,
                        "measured_rounds": 5,
                        "compiler_route_cost_ratio_bounds": [
                            COMPILER_ROUTE_MIN,
                            COMPILER_ROUTE_MAX,
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        raw_root = args.raw_root.resolve()
        raw_root.mkdir(parents=True, exist_ok=True)
        if args.stage == "measure":
            implementation = None
            unialloc_implementation_sha256 = None
            implementation_source = None
            wrapper = None
            sysroot = None
        else:
            implementation, unialloc_implementation_sha256 = (
                freeze_unialloc_implementation(
                    raw_root, requested_implementation_revision
                )
            )
            implementation_source = json.loads(
                (implementation / "snapshot.json").read_text(encoding="utf-8")
            )
            if not args.diagnostic_current_worktree:
                validate_primary_implementation(
                    implementation_revision, unialloc_implementation_sha256
                )
            wrapper = ensure_frozen_wrapper(
                implementation,
                unialloc_implementation_sha256,
                raw_root=raw_root,
                toolchain=args.toolchain,
                timeout=args.build_timeout,
            )
            sysroot = matrix.rustc_sysroot(args.toolchain)
        paths = [
            run_target(
                TARGETS[target_id],
                raw_root=raw_root,
                implementation=implementation,
                unialloc_implementation_sha256=unialloc_implementation_sha256,
                toolchain=args.toolchain,
                jobs=args.jobs,
                build_timeout=args.build_timeout,
                run_timeout=args.run_timeout,
                cpu_list=args.cpu_list,
                numa_node=args.numa_node,
                wrapper=wrapper,
                sysroot=sysroot,
                implementation_revision=implementation_revision,
                stage=args.stage,
                diagnostic_current_worktree=args.diagnostic_current_worktree,
                implementation_source=implementation_source,
            )
            for target_id in args.targets
        ]
        if args.stage != "preflight" and not args.diagnostic_current_worktree:
            publish_target_results(paths)
    except (
        CampaignError,
        matrix.MatrixError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps({"success": True, "target_results": [str(path) for path in paths]})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
