#!/usr/bin/env python3
"""Convert cargo bench benchmark output into one JSON timing record.

This helper is intentionally narrow: it does not invent timing from wall-clock
process duration.  It only emits a successful timing record when the child
command prints libtest benchmark rows with finite ``ns/iter`` values or writes
Criterion ``target/criterion/**/new/estimates.json`` files with finite mean
point estimates.  That makes it useful as a bridge from source-discovered
``cargo bench`` surfaces to the strict external-workload adapter, while still
leaving claim-grade status to the outer manifest/config gates.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import paper_workload_driver as _paper_workload_driver
except ModuleNotFoundError as exc:
    if exc.name != "paper_workload_driver":
        raise
    _driver_path = Path(__file__).resolve().with_name("paper_workload_driver.py")
    _driver_spec = importlib.util.spec_from_file_location("paper_workload_driver", _driver_path)
    if _driver_spec is None or _driver_spec.loader is None:  # pragma: no cover - installation failure
        raise ImportError(f"could not load canonical Scudo driver from {_driver_path}") from exc
    _paper_workload_driver = importlib.util.module_from_spec(_driver_spec)
    _driver_spec.loader.exec_module(_paper_workload_driver)

BENCH_LINE = re.compile(
    r"^test (?P<name>\S+)\s+\.\.\. bench:\s+"
    r"(?P<ns>[0-9][0-9,]*(?:\.[0-9]+)?)\s+ns/iter"
    r"(?:\s+\(\+/-\s+(?P<dev>[0-9][0-9,]*(?:\.[0-9]+)?)\))?"
)

ALLOCATOR_FEATURES = {
    "default": "bench_ptmalloc",
    "system": "bench_ptmalloc",
    "ptmalloc": "bench_ptmalloc",
    "ourself": "bench_ourself",
    "unialloc": "bench_ourself",
    "jemalloc": "bench_jemalloc",
    "mimalloc": "bench_mimalloc",
    "tcmalloc": "bench_tcmalloc",
    "snmalloc": "bench_snmalloc",
    "scudo": "bench_scudo",
}

ALLOCATOR_CARGO_OVERLAY_MARKER = "# UniAlloc evaluation external cargo allocator overlay."
ALLOCATOR_OLD_NIGHTLY_PIN_MARKER = "# UniAlloc evaluation external cargo old-nightly dependency pins."
ALLOCATOR_BENCH_OVERLAY_MARKER = "// UniAlloc evaluation external cargo allocator overlay."
ALLOCATOR_BENCH_OVERLAY_END_MARKER = "// End UniAlloc evaluation external cargo allocator overlay."
SCUDO_RUNTIME_IDENTITY_MARKER = _paper_workload_driver.SCUDO_RUNTIME_IDENTITY_MARKER

POLARS_JSONPATH_STALE_GIT_DEP = (
    'jsonpath_lib = { version = "0.3.0", optional = true, '
    'git = "https://github.com/ritchie46/jsonpath", branch = "improve_compiled" }'
)
POLARS_JSONPATH_CRATES_IO_DEP = 'jsonpath_lib = { version = "0.3.0", optional = true }'


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def emit(record: Dict[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True))


def forward_interrupted_child_output(result: Dict[str, Any]) -> None:
    """Forward bounded child output before emitting our Ctrl-C diagnostic."""

    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()


def cargo_child_interruption_record(
    args: argparse.Namespace,
    command: List[str],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-interruption",
        "status": "interrupted",
        "success": False,
        "interrupted": True,
        "diagnostic_only": True,
        "claim_grade": False,
        "claim_grade_blockers": [
            "child cargo bench command interrupted before complete measurement"
        ],
        "measurement_eligible": False,
        "import_eligible": False,
        "sample_persisted": False,
        "seconds": None,
        "dataset": getattr(args, "dataset", None),
        "benchmark": getattr(args, "benchmark", None),
        "allocator": getattr(args, "allocator", None),
        "variant_feature": getattr(args, "variant_feature", None),
        "run_index": getattr(args, "run_index", None),
        "command": command,
        "exit_code": 130,
        "child_returncode_after_cleanup": result.get("child_returncode_after_cleanup"),
        "interrupt_signal": result.get("interrupt_signal") or "SIGINT",
        "process_group_pid": result.get("process_group_pid"),
        "process_group_terminated": bool(result.get("process_group_terminated")),
        "process_group_absent_after_cleanup": bool(result.get("process_group_absent_after_cleanup")),
        "timed_out": False,
        "timeout_seconds": getattr(args, "timeout", None),
        "max_output_bytes": result.get("max_output_bytes"),
        "stdout_bytes": result.get("stdout_bytes"),
        "stderr_bytes": result.get("stderr_bytes"),
        "stdout_retained_bytes": result.get("stdout_retained_bytes"),
        "stderr_retained_bytes": result.get("stderr_retained_bytes"),
        "stdout_truncated": result.get("stdout_truncated"),
        "stderr_truncated": result.get("stderr_truncated"),
        "started_at": result.get("started_at"),
        "ended_at": result.get("ended_at") or now_iso(),
        "generated_at": now_iso(),
    }


def finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def positive_int_value(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def env_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def geomean(values: Iterable[float]) -> Optional[float]:
    xs = [float(value) for value in values if finite_number(value) is not None and float(value) > 0]
    if not xs:
        return None
    return math.exp(sum(math.log(value) for value in xs) / len(xs))


def parse_bench_rows(text: str, *, name_filter: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        match = BENCH_LINE.match(raw.strip())
        if not match:
            continue
        name = match.group("name")
        if name_filter and name_filter not in name:
            continue
        ns_text = match.group("ns").replace(",", "")
        ns: float | int = float(ns_text) if "." in ns_text else int(ns_text)
        dev_raw = match.group("dev")
        dev_text = dev_raw.replace(",", "") if dev_raw else ""
        rows.append(
            {
                "name": name,
                "ns_per_iter": ns,
                "stddev_ns_per_iter": (
                    float(dev_text) if "." in dev_text else int(dev_text)
                )
                if dev_text
                else None,
                "measurement_source": "libtest_bench_ns_per_iter",
                "raw": raw.strip(),
            }
        )
    return rows


def nested_number(record: Dict[str, Any], path: Iterable[str]) -> Optional[float]:
    value: Any = record
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return finite_number(value)


def criterion_row_name(path: Path, criterion_dir: Path) -> str:
    try:
        rel = path.relative_to(criterion_dir)
    except ValueError:
        return str(path)
    parts = list(rel.parts)
    if len(parts) >= 3 and parts[-2:] == ["new", "estimates.json"]:
        parts = parts[:-2]
    elif parts and parts[-1] == "estimates.json":
        parts = parts[:-1]
    return "/".join(parts) or str(rel)


def parse_criterion_estimates(
    criterion_dir: Path,
    *,
    started_at: float,
    name_filter: str = "",
    include_stale: bool = False,
) -> List[Dict[str, Any]]:
    if not criterion_dir.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(criterion_dir.glob("**/new/estimates.json")):
        try:
            stat = path.stat()
        except OSError:
            continue
        if not include_stale and stat.st_mtime < started_at - 1.0:
            continue
        name = criterion_row_name(path, criterion_dir)
        if name_filter and name_filter not in name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ns = nested_number(data, ["mean", "point_estimate"])
        if ns is None:
            ns = nested_number(data, ["median", "point_estimate"])
        if ns is None or ns <= 0:
            continue
        stddev = nested_number(data, ["std_dev", "point_estimate"])
        rows.append(
            {
                "name": name,
                "ns_per_iter": ns,
                "stddev_ns_per_iter": stddev,
                "measurement_source": "criterion_estimates_json",
                "criterion_estimates_path": str(path),
            }
        )
    return rows


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def prepend_env_path(env: Dict[str, str], key: str, values: Iterable[Path]) -> List[str]:
    prefixes = [str(path) for path in values if path.exists()]
    if not prefixes:
        return []
    existing = env.get(key, "")
    env[key] = os.pathsep.join(prefixes + ([existing] if existing else []))
    return prefixes


def prepend_env_flags(env: Dict[str, str], key: str, flags: Iterable[str]) -> List[str]:
    existing = env.get(key, "")
    existing_parts = existing.split()
    added: List[str] = []
    for flag in flags:
        if flag and flag not in existing_parts and flag not in added:
            added.append(flag)
    if added:
        env[key] = " ".join(added + existing_parts).strip()
    return added


def first_existing_file(paths: Iterable[Path]) -> Optional[Path]:
    for path in paths:
        if path.exists() and path.is_file():
            return path
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileMutationJournal:
    """Snapshot and restore source files that the cargo-bench adapter mutates.

    External paper workload checkouts are provenance artifacts.  Allocator
    feature routing necessarily overlays Cargo features and benchmark crate
    roots before running a selected child benchmark, but those edits must not
    leak into later samples.  This journal records the exact pre-mutation bytes
    for every file touched by the adapter and restores them on every exit path.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._errors: List[Dict[str, Any]] = []
        self._restored = False

    def snapshot(self, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError as exc:
            self._errors.append({"path": str(path), "stage": "snapshot_resolve", "error": str(exc)})
            return
        key = str(resolved)
        if key in self._snapshots:
            return
        try:
            resolved.relative_to(self.root)
        except ValueError:
            self._errors.append(
                {
                    "path": key,
                    "stage": "snapshot_scope",
                    "error": f"refusing to snapshot file outside real workload dir {self.root}",
                }
            )
            return
        existed = resolved.exists()
        data: Optional[bytes] = None
        if existed:
            try:
                data = resolved.read_bytes()
            except OSError as exc:
                self._errors.append({"path": key, "stage": "snapshot_read", "error": str(exc)})
                return
        self._snapshots[key] = {
            "path": key,
            "existed": existed,
            "sha256_before": sha256_bytes(data) if data is not None else None,
            "bytes_before": data,
        }

    def restore(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "schema_version": 1,
            "source": "paper-external-cargo-bench-json-checkout-cleanup",
            "real_workload_dir": str(self.root),
            "tracked_file_count": len(self._snapshots),
            "restored": [],
            "errors": list(self._errors),
            "success": True,
            "already_restored": self._restored,
        }
        if self._restored:
            record["success"] = not record["errors"]
            return record
        for snapshot in self._snapshots.values():
            path = Path(str(snapshot["path"]))
            existed = bool(snapshot["existed"])
            before = snapshot.get("bytes_before")
            entry: Dict[str, Any] = {
                "path": str(path),
                "existed_before": existed,
                "sha256_before": snapshot.get("sha256_before"),
                "restored": False,
            }
            try:
                after_before_restore = path.read_bytes() if path.exists() else None
                entry["sha256_at_cleanup_start"] = (
                    sha256_bytes(after_before_restore) if after_before_restore is not None else None
                )
                if existed:
                    assert isinstance(before, bytes)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(before)
                    entry["restored"] = True
                    entry["sha256_after"] = sha256_bytes(path.read_bytes())
                elif path.exists():
                    path.unlink()
                    entry["deleted_created_file"] = True
                    entry["restored"] = True
                    entry["sha256_after"] = None
                else:
                    entry["restored"] = True
                    entry["sha256_after"] = None
            except Exception as exc:  # pragma: no cover - defensive fail-closed evidence path
                error = {"path": str(path), "stage": "restore", "error": str(exc)}
                entry["error"] = str(exc)
                record["errors"].append(error)
            record["restored"].append(entry)
        self._restored = True
        record["success"] = not record["errors"] and all(bool(item.get("restored")) for item in record["restored"])
        return record


def discover_local_cmake_bins(repo_root: Path) -> List[Path]:
    raw_root = repo_root / "evaluation" / "raw"
    home = Path.home()
    candidates: List[Path] = []
    env_cmake = os.environ.get("UNIALLOC_CMAKE_BIN") or os.environ.get("CMAKE")
    if env_cmake:
        raw = Path(env_cmake).expanduser()
        candidates.append(raw.parent if raw.name == "cmake" else raw)
    candidates.extend(sorted(raw_root.glob("local-cmake-pip-*/bin")))
    candidates.extend(sorted(raw_root.glob("local-cmake-pip-*/cmake/data/bin")))
    candidates.extend(
        [
            home / "Library" / "Python" / f"{sys.version_info.major}.{sys.version_info.minor}" / "bin",
            home / "Library" / "Python" / "3.9" / "bin",
            Path("/opt/homebrew/opt/cmake/bin"),
        ]
    )
    seen = set()
    result: List[Path] = []
    for path in candidates:
        path = path.expanduser()
        if path in seen:
            continue
        seen.add(path)
        if (path / "cmake").exists():
            result.append(path)
    return result


def discover_tcmalloc_lib_dirs(repo_root: Path) -> List[Path]:
    raw_root = repo_root / "evaluation" / "raw"
    candidates: List[Path] = []
    env_lib_dir = os.environ.get("UNIALLOC_TCMALLOC_LIB_DIR")
    if env_lib_dir:
        candidates.append(Path(env_lib_dir).expanduser())
    candidates.extend(
        [
            repo_root / "evaluation" / "deps" / "tcmalloc" / "lib",
            repo_root
            / "evaluation"
            / "external"
            / "_deps"
            / "homebrew-cellar"
            / "gperftools"
            / "2.18.1"
            / "lib",
            Path("/opt/homebrew/opt/gperftools/lib"),
        ]
    )
    candidates.extend(sorted(raw_root.glob("local-tcmalloc-dep-build-*/prefix/lib")))
    candidates.extend(sorted(raw_root.glob("local-tcmalloc-dep-build-*/gperftools-*/.libs")))
    seen = set()
    result: List[Path] = []
    for path in candidates:
        path = path.expanduser()
        if path in seen:
            continue
        seen.add(path)
        if first_existing_file(
            [
                path / "libtcmalloc.dylib",
                path / "libtcmalloc.a",
                path / "libtcmalloc_minimal.dylib",
                path / "libtcmalloc_minimal.a",
            ]
        ):
            result.append(path)
    return result


def discover_tcmalloc_include_dirs(repo_root: Path) -> List[Path]:
    raw_root = repo_root / "evaluation" / "raw"
    candidates: List[Path] = [
        repo_root / "evaluation" / "deps" / "tcmalloc" / "include",
        repo_root
        / "evaluation"
        / "external"
        / "_deps"
        / "homebrew-cellar"
        / "gperftools"
        / "2.18.1"
        / "include",
        Path("/opt/homebrew/opt/gperftools/include"),
    ]
    candidates.extend(sorted(raw_root.glob("local-tcmalloc-dep-build-*/prefix/include")))
    candidates.extend(sorted(raw_root.glob("local-tcmalloc-dep-build-*/gperftools-*/src")))
    seen = set()
    result: List[Path] = []
    for path in candidates:
        path = path.expanduser()
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            result.append(path)
    return result


POLARS_GROUPBY_REQUIRED_CSV_COLUMNS = ["id1", "id2", "id3", "id4", "id5", "id6", "v1", "v2", "v3"]
RPOLARS_PAPER_CARGO_BENCH_TARGETS = {"bench", "csv", "groupby", "collect", "take", "sort"}
RPOLARS_CANONICAL_GROUPBY_CSV_NAME = "G1_1e7_1e2_5_0.csv"
RPOLARS_CANONICAL_GROUPBY_ROWS = 10_000_000
RPOLARS_CANONICAL_INPUT_SOURCE = "polars-db-benchmark-groupby-datagen"


def read_csv_header_columns(path: Path) -> List[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            header = handle.readline().strip()
    except OSError:
        return []
    return [part.strip().strip('"') for part in header.split(",") if part.strip()]


def csv_data_row_count(path: Path) -> Optional[int]:
    """Count data rows in a CSV without loading the paper-sized input in memory."""

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            # Skip the header.  Keep blank trailing lines from inflating the
            # paper-input contract, but do not parse CSV values here: the
            # schema contract is checked separately from the streaming row count.
            next(handle, None)
            return sum(1 for line in handle if line.strip())
    except OSError:
        return None


def int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def rpolars_actual_csv_src(record: Dict[str, Any]) -> Optional[Path]:
    dependency_env = record.get("dependency_env") if isinstance(record.get("dependency_env"), dict) else {}
    candidates: List[Any] = [dependency_env.get("csv_src")]
    fixture = dependency_env.get("polars_csv_fixture") if isinstance(dependency_env.get("polars_csv_fixture"), dict) else {}
    candidates.extend([fixture.get("selected_csv_src"), fixture.get("previous_csv_src")])
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return Path(text)
    return None


def rpolars_input_contract_sources(contract: Dict[str, Any]) -> Dict[str, Any]:
    configured_csv = contract.get("configured_csv") if isinstance(contract.get("configured_csv"), dict) else {}
    input_contract = contract.get("input_contract") if isinstance(contract.get("input_contract"), dict) else {}
    expected_datagen_args = (
        contract.get("expected_datagen_args") if isinstance(contract.get("expected_datagen_args"), dict) else {}
    )
    db_source = contract.get("db_benchmark_source") if isinstance(contract.get("db_benchmark_source"), dict) else {}
    canonical_datagen_args = (
        db_source.get("canonical_datagen_args") if isinstance(db_source.get("canonical_datagen_args"), dict) else {}
    )
    return {
        "configured_csv": configured_csv,
        "input_contract": input_contract,
        "expected_datagen_args": expected_datagen_args,
        "db_source": db_source,
        "canonical_datagen_args": canonical_datagen_args,
    }


def rpolars_validate_input_contract_against_csv(
    contract: Dict[str, Any],
    actual_csv: Optional[Path],
    *,
    previous_columns: Optional[List[str]] = None,
) -> List[str]:
    """Validate that the wrapper used the audited canonical R-Polars input.

    This is deliberately stricter than "the CSV has the right header": the
    paper-equivalent groupby target needs the db-benchmark input path, basename,
    hash, row count, and source contract to match the plan-generated audit.
    """

    if not contract:
        return ["R-Polars input provenance contract is missing"]
    if contract.get("_parse_error"):
        return [f"R-Polars input provenance contract is invalid: {contract.get('_parse_error')}"]

    parts = rpolars_input_contract_sources(contract)
    configured_csv = parts["configured_csv"]
    input_contract = parts["input_contract"]
    expected_datagen_args = parts["expected_datagen_args"]
    db_source = parts["db_source"]
    canonical_datagen_args = parts["canonical_datagen_args"]
    blockers: List[str] = []

    if contract.get("source") != "rpolars-input-provenance-audit":
        blockers.append("R-Polars input provenance contract was not produced by the plan input audit")
    if contract.get("required_for_targets") is not True:
        blockers.append("R-Polars input provenance contract does not cover csv/groupby targets")
    if contract.get("claim_grade_ready") is not True:
        blockers.append("R-Polars input provenance contract is not claim_grade_ready=true")
    nested = contract.get("blockers")
    if isinstance(nested, list):
        blockers.extend(f"R-Polars input provenance blocker: {item}" for item in nested if str(item).strip())
    elif nested:
        blockers.append(f"R-Polars input provenance blocker: {nested}")
    if configured_csv.get("exists") is not True:
        blockers.append("R-Polars input provenance configured_csv does not exist")
    if configured_csv.get("missing_columns"):
        blockers.append("R-Polars input provenance configured_csv is missing required columns")

    expected_source = str(
        contract.get("expected_source")
        or input_contract.get("source")
        or db_source.get("source")
        or ""
    ).strip()
    if expected_source != RPOLARS_CANONICAL_INPUT_SOURCE:
        blockers.append(
            "R-Polars input provenance source is not the canonical db-benchmark generator"
            f": {expected_source or '<missing>'}"
        )
    db_source_id = str(db_source.get("source") or "").strip()
    if db_source and db_source_id != RPOLARS_CANONICAL_INPUT_SOURCE:
        blockers.append(
            "R-Polars db-benchmark source record is not canonical"
            f": {db_source_id or '<missing>'}"
        )

    expected_name = str(
        contract.get("expected_csv_name")
        or input_contract.get("expected_csv_name")
        or configured_csv.get("basename")
        or ""
    ).strip()
    if expected_name != RPOLARS_CANONICAL_GROUPBY_CSV_NAME:
        blockers.append(
            "R-Polars input provenance expected CSV is not canonical"
            f": {expected_name or '<missing>'} != {RPOLARS_CANONICAL_GROUPBY_CSV_NAME}"
        )
    configured_basename = str(configured_csv.get("basename") or "").strip()
    if configured_basename and configured_basename != RPOLARS_CANONICAL_GROUPBY_CSV_NAME:
        blockers.append(
            "R-Polars configured CSV basename is not canonical"
            f": {configured_basename} != {RPOLARS_CANONICAL_GROUPBY_CSV_NAME}"
        )

    expected_rows = (
        int_or_none(expected_datagen_args.get("n_rows"))
        or int_or_none(canonical_datagen_args.get("n_rows"))
        or int_or_none(input_contract.get("expected_rows"))
    )
    if expected_rows != RPOLARS_CANONICAL_GROUPBY_ROWS:
        blockers.append(
            "R-Polars input provenance expected row count is not canonical"
            f": {expected_rows if expected_rows is not None else '<missing>'} != {RPOLARS_CANONICAL_GROUPBY_ROWS}"
        )
    configured_rows = int_or_none(configured_csv.get("data_row_count"))
    if configured_rows != RPOLARS_CANONICAL_GROUPBY_ROWS:
        blockers.append(
            "R-Polars configured CSV row count is not canonical"
            f": {configured_rows if configured_rows is not None else '<missing>'} != {RPOLARS_CANONICAL_GROUPBY_ROWS}"
        )
    contract_rows = int_or_none(input_contract.get("expected_rows"))
    if input_contract and contract_rows != RPOLARS_CANONICAL_GROUPBY_ROWS:
        blockers.append(
            "R-Polars input contract row count is not canonical"
            f": {contract_rows if contract_rows is not None else '<missing>'} != {RPOLARS_CANONICAL_GROUPBY_ROWS}"
        )

    expected_sha = str(configured_csv.get("sha256") or input_contract.get("csv_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        blockers.append("R-Polars input provenance configured_csv lacks verified sha256")

    configured_path_text = str(
        configured_csv.get("resolved_path") or configured_csv.get("configured_csv_src") or ""
    ).strip()
    if not configured_path_text:
        blockers.append("R-Polars input provenance configured_csv lacks a resolved path")
    if actual_csv is None:
        blockers.append("R-Polars wrapper did not record actual CSV_SRC used by the child command")
        return unique_strings(blockers)

    try:
        actual_resolved = actual_csv.expanduser().resolve()
    except OSError:
        blockers.append(f"R-Polars actual CSV_SRC cannot be resolved: {actual_csv}")
        return unique_strings(blockers)
    if not actual_resolved.exists():
        blockers.append(f"R-Polars actual CSV_SRC does not exist: {actual_resolved}")
        return unique_strings(blockers)
    if actual_resolved.name != RPOLARS_CANONICAL_GROUPBY_CSV_NAME:
        blockers.append(
            "R-Polars actual CSV_SRC basename is not canonical"
            f": {actual_resolved.name} != {RPOLARS_CANONICAL_GROUPBY_CSV_NAME}"
        )
    if configured_path_text:
        try:
            configured_resolved = Path(configured_path_text).expanduser().resolve()
            if configured_resolved != actual_resolved:
                blockers.append(
                    "R-Polars actual CSV_SRC does not match the audited configured_csv path"
                    f": {actual_resolved} != {configured_resolved}"
                )
        except OSError:
            blockers.append(f"R-Polars configured CSV path cannot be resolved: {configured_path_text}")

    actual_columns = previous_columns if previous_columns is not None else read_csv_header_columns(actual_resolved)
    missing_columns = [col for col in POLARS_GROUPBY_REQUIRED_CSV_COLUMNS if col not in set(actual_columns)]
    if missing_columns:
        blockers.append("R-Polars actual CSV_SRC is missing required columns: " + ", ".join(missing_columns))
    actual_rows = csv_data_row_count(actual_resolved)
    if actual_rows != RPOLARS_CANONICAL_GROUPBY_ROWS:
        blockers.append(
            "R-Polars actual CSV_SRC row count is not canonical"
            f": {actual_rows if actual_rows is not None else '<unreadable>'} != {RPOLARS_CANONICAL_GROUPBY_ROWS}"
        )
    if expected_sha:
        try:
            actual_sha = file_sha256(actual_resolved)
            if actual_sha != expected_sha:
                blockers.append(
                    "R-Polars actual CSV_SRC sha256 does not match the audited input contract"
                    f": {actual_sha} != {expected_sha}"
                )
        except OSError:
            blockers.append(f"R-Polars actual CSV_SRC sha256 could not be computed: {actual_resolved}")
    return unique_strings(blockers)


def write_polars_groupby_compat_csv(path: Path, rows: int = 4096) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(",".join(POLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + "\n")
        for index in range(rows):
            values = [
                f"a{index % 97}",
                f"b{index % 89}",
                f"c{index % 83}",
                f"d{index % 79}",
                f"e{index % 73}",
                f"f{index % 67}",
                str((index * 17) % 100003),
                f"{((index * 31) % 10007) / 10.0:.1f}",
                f"{((index * 43) % 10009) / 100.0:.2f}",
            ]
            handle.write(",".join(values) + "\n")
    return {
        "path": str(path),
        "row_count": rows,
        "columns": POLARS_GROUPBY_REQUIRED_CSV_COLUMNS,
    }


def rpolars_existing_csv_claim_grade_record(
    *,
    args: argparse.Namespace,
    previous: Optional[Path],
    previous_columns: List[str],
) -> Optional[Dict[str, Any]]:
    """Return claim-grade provenance for an existing R-Polars CSV_SRC if valid.

    The wrapper has two very different CSV paths:
    * a generated compatibility fixture, useful only to keep the historical
      upstream benchmark runnable; and
    * the canonical db-benchmark CSV already audited by the plan generator.

    Treat the second case as claim-grade only when the plan-provided input
    provenance contract names the same resolved path and sha256.  A mere schema
    match is intentionally not enough, because a tiny compatibility CSV has the
    same columns but not the paper-equivalent input size/provenance.
    """

    if previous is None:
        return None
    contract = json_arg_object(getattr(args, "rpolars_input_contract_json", ""))
    if not contract or contract.get("_parse_error"):
        return None
    if contract.get("source") != "rpolars-input-provenance-audit":
        return None
    if contract.get("claim_grade_ready") is not True:
        return None
    if contract.get("required_for_targets") is not True:
        return None
    if contract.get("blockers"):
        return None
    blockers = rpolars_validate_input_contract_against_csv(
        contract,
        previous,
        previous_columns=previous_columns,
    )
    if blockers:
        return None
    configured_csv = contract.get("configured_csv") if isinstance(contract.get("configured_csv"), dict) else {}
    input_contract = contract.get("input_contract") if isinstance(contract.get("input_contract"), dict) else {}
    expected_sha = str(configured_csv.get("sha256") or input_contract.get("csv_sha256") or "").strip().lower()
    return {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-polars-csv-fixture",
        "benchmark": "R-Polars",
        "required_columns": POLARS_GROUPBY_REQUIRED_CSV_COLUMNS,
        "previous_csv_src": str(previous),
        "previous_columns": previous_columns,
        "selected_csv_src": str(previous),
        "changed": False,
        "claim_grade": True,
        "reason": "existing CSV_SRC matches the claim-grade R-Polars input provenance contract",
        "input_contract_sha256": expected_sha,
        "input_contract_rows": configured_csv.get("data_row_count"),
    }


def maybe_prepare_polars_csv_fixture(
    args: argparse.Namespace,
    env: Dict[str, str],
    *,
    requested_targets: List[str],
) -> Optional[Dict[str, Any]]:
    if str(args.benchmark or "") != "R-Polars":
        return None
    if requested_targets and "groupby" not in requested_targets:
        return None
    real_workload_dir = Path(args.real_workload_dir).expanduser().resolve() if args.real_workload_dir else Path.cwd()
    if not (real_workload_dir / "polars" / "benches" / "groupby.rs").exists():
        return None

    required = set(POLARS_GROUPBY_REQUIRED_CSV_COLUMNS)
    previous = Path(env.get("CSV_SRC", "")).expanduser() if env.get("CSV_SRC") else None
    previous_columns = read_csv_header_columns(previous) if previous else []
    previous_satisfies = required.issubset(set(previous_columns))
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-polars-csv-fixture",
        "benchmark": "R-Polars",
        "required_columns": POLARS_GROUPBY_REQUIRED_CSV_COLUMNS,
        "previous_csv_src": str(previous) if previous else None,
        "previous_columns": previous_columns,
        "changed": False,
        "claim_grade": False,
        "not_claim_grade_reason": (
            "synthetic R-Polars compatibility CSV only; claim-grade paper evidence requires "
            "paper-equivalent raw input provenance"
        ),
    }
    if previous_satisfies:
        claim_grade_record = rpolars_existing_csv_claim_grade_record(
            args=args,
            previous=previous,
            previous_columns=previous_columns,
        )
        if claim_grade_record is not None:
            return claim_grade_record
        contract = json_arg_object(getattr(args, "rpolars_input_contract_json", ""))
        contract_blockers = rpolars_validate_input_contract_against_csv(
            contract,
            previous,
            previous_columns=previous_columns,
        ) if contract else []
        if contract_blockers:
            record["input_contract_blockers"] = contract_blockers
            record["not_claim_grade_reason"] = "; ".join(contract_blockers[:3])
        record["selected_csv_src"] = str(previous)
        record["reason"] = "existing CSV_SRC satisfies the R-Polars groupby benchmark schema"
        return record

    fixture = repo_root_from_script() / "evaluation" / "raw" / "rpolars-generated-fixtures" / "polars_groupby_compat.csv"
    fixture_columns = read_csv_header_columns(fixture)
    if not required.issubset(set(fixture_columns)):
        fixture_info = write_polars_groupby_compat_csv(fixture)
        record["generated_fixture"] = fixture_info
        record["changed"] = True
    else:
        record["generated_fixture"] = {
            "path": str(fixture),
            "columns": fixture_columns,
        }
    env["CSV_SRC"] = str(fixture)
    record["selected_csv_src"] = str(fixture)
    record["reason"] = "replaced CSV_SRC because the configured file is missing R-Polars groupby columns"
    return record


def configure_dependency_env(args: argparse.Namespace, env: Dict[str, str]) -> Dict[str, Any]:
    repo_root = repo_root_from_script()
    allocator = normalize_allocator_name(args.allocator)
    real_workload_dir = Path(args.real_workload_dir).expanduser().resolve() if args.real_workload_dir else Path.cwd()
    previous_registry_protocol = env.get("CARGO_REGISTRIES_CRATES_IO_PROTOCOL")
    previous_net_retry = env.get("CARGO_NET_RETRY")
    previous_net_offline = env.get("CARGO_NET_OFFLINE")
    allow_network = env_truthy(env.get("UNIALLOC_PAPER_CARGO_ALLOW_NETWORK"))
    env.setdefault("CARGO_REGISTRIES_CRATES_IO_PROTOCOL", "sparse")
    env.setdefault("CARGO_NET_RETRY", "2")
    if not allow_network:
        env.setdefault("CARGO_NET_OFFLINE", "true")
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-dependency-env",
        "allocator": allocator or None,
        "cargo_network_allowed": allow_network,
        "cargo_registry_protocol": env.get("CARGO_REGISTRIES_CRATES_IO_PROTOCOL"),
        "cargo_registry_protocol_defaulted": previous_registry_protocol is None,
        "cargo_net_retry": env.get("CARGO_NET_RETRY"),
        "cargo_net_retry_defaulted": previous_net_retry is None,
        "cargo_net_offline": env.get("CARGO_NET_OFFLINE"),
        "cargo_net_offline_defaulted": (not allow_network) and previous_net_offline is None,
        "path_prefixes": [],
        "library_path_prefixes": [],
        "dyld_fallback_library_path_prefixes": [],
        "cflags_prefix": "",
        "cxxflags_prefix": "",
        "ldflags_prefix": "",
        "rustflags_prefix": "",
    }
    cmake_bins = discover_local_cmake_bins(repo_root)
    if allocator == "snmalloc":
        record["path_prefixes"] = prepend_env_path(env, "PATH", cmake_bins)
        if record["path_prefixes"]:
            record["cmake"] = str(Path(record["path_prefixes"][0]) / "cmake")
    elif cmake_bins:
        record["available_cmake_bins"] = [str(path) for path in cmake_bins]
    if allocator == "tcmalloc":
        lib_dirs = discover_tcmalloc_lib_dirs(repo_root)
        include_dirs = discover_tcmalloc_include_dirs(repo_root)
        record["library_path_prefixes"] = prepend_env_path(env, "LIBRARY_PATH", lib_dirs)
        record["dyld_fallback_library_path_prefixes"] = prepend_env_path(
            env,
            "DYLD_FALLBACK_LIBRARY_PATH",
            lib_dirs,
        )
        include_flags = " ".join(f"-I{path}" for path in include_dirs if path.exists())
        library_flags = " ".join(f"-L{path}" for path in lib_dirs if path.exists())
        if include_flags:
            env["CFLAGS"] = (include_flags + " " + env.get("CFLAGS", "")).strip()
            env["CXXFLAGS"] = (include_flags + " " + env.get("CXXFLAGS", "")).strip()
            record["cflags_prefix"] = include_flags
            record["cxxflags_prefix"] = include_flags
        if library_flags:
            env["LDFLAGS"] = (library_flags + " " + env.get("LDFLAGS", "")).strip()
            record["ldflags_prefix"] = library_flags
        record["tcmalloc_lib_dirs"] = [str(path) for path in lib_dirs]
        record["tcmalloc_include_dirs"] = [str(path) for path in include_dirs]
    local_toolchain = read_local_rust_toolchain(real_workload_dir)
    inherited_toolchain = os.environ.get("RUSTUP_TOOLCHAIN", "")
    effective_toolchain = inherited_toolchain or local_toolchain
    if "nightly-2021" in effective_toolchain:
        # These paper-era checkouts pin early 2021 nightly toolchains, but
        # modern registry resolution can still pull crates declaring edition
        # 2021, which that compiler only accepts behind the historical rustc
        # unstable-options gate.  On current macOS/arm64 linkers, the same old
        # Rust std rlibs can also fail because ld no longer ignores lib.rmeta
        # archive members; routing through ld_classic preserves the legacy
        # toolchain behavior while keeping the wrapper evidence non-claim-grade.
        rustflags = ["-Z", "unstable-options"]
        if sys.platform == "darwin":
            rustflags.append("-C")
            rustflags.append("link-arg=-Wl,-ld_classic")
        added_rustflags = prepend_env_flags(env, "RUSTFLAGS", rustflags)
        record.update(
            {
                "legacy_rust_toolchain": local_toolchain or inherited_toolchain,
                "effective_rust_toolchain_for_legacy_env": effective_toolchain,
                "legacy_rustflags_added": added_rustflags,
                "rustflags_prefix": " ".join(added_rustflags),
            }
        )
    elif inherited_toolchain:
        record.update(
            {
                "local_rust_toolchain": local_toolchain or None,
                "inherited_rust_toolchain": inherited_toolchain,
                "effective_rust_toolchain_for_legacy_env": effective_toolchain,
                "legacy_rustflags_added": [],
            }
        )
    return record


def normalize_allocator_name(raw: str) -> str:
    return str(raw or "").strip().lower().replace("_", "-")


def allocator_feature(allocator: str) -> str:
    return ALLOCATOR_FEATURES.get(normalize_allocator_name(allocator), "")


def cargo_command_features(command: Iterable[str]) -> List[str]:
    """Return explicitly requested Cargo features from a child command."""

    args = [str(part) for part in command]
    values: List[str] = []
    for index, token in enumerate(args):
        if token in {"--features", "--feature"} and index + 1 < len(args):
            values.append(args[index + 1])
        elif token.startswith("--features="):
            values.append(token.split("=", 1)[1])
        elif token.startswith("--feature="):
            values.append(token.split("=", 1)[1])
    return unique_strings(
        feature
        for value in values
        for feature in re.split(r"[,\s]+", value)
        if feature
    )


def command_requests_scudo_feature(command: Iterable[str]) -> bool:
    return "bench_scudo" in cargo_command_features(command)


def scudo_request_sources(args: argparse.Namespace, command: Iterable[str]) -> List[str]:
    sources: List[str] = []
    if allocator_feature(args.allocator) == "bench_scudo":
        sources.append("allocator-selector")
    if command_requests_scudo_feature(command):
        sources.append("cargo-feature")
    return sources


def canonical_scudo_driver_args(
    args: argparse.Namespace,
    command: List[str],
    real_workload_dir: Path,
) -> argparse.Namespace:
    """Build the narrow Namespace required by the canonical Scudo driver."""

    cached = getattr(args, "_external_scudo_driver_args", None)
    if isinstance(cached, argparse.Namespace):
        return cached
    effective_toolchain = effective_rust_toolchain_for_workload(real_workload_dir, command)
    driver_args = argparse.Namespace(
        allocator="scudo",
        # The canonical driver uses None to mean the UniAlloc repository pin.
        # An external checkout with no explicit/local toolchain instead runs
        # the PATH/system cargo, so preserve that route with its explicit alias.
        rust_toolchain=effective_toolchain or "system",
        scudo_probe_cwd=str(real_workload_dir),
        extra_feature=[],
        compiler_site_replay_type_mapping=None,
        compiler_site_id_mode="cyclic-replay",
        compiler_site_recovery_scope="thread-local",
        compiler_site_replay_limit=_paper_workload_driver.COMPILER_SITE_REPLAY_DEFAULT_LIMIT,
        tcmalloc_lib_dir=None,
        cmake_bin=None,
        scudo_mode=str(
            getattr(args, "scudo_mode", _paper_workload_driver.SCUDO_DEFAULT_MODE)
            or _paper_workload_driver.SCUDO_DEFAULT_MODE
        ),
        scudo_runtime_library=getattr(args, "scudo_runtime_library", None),
    )
    setattr(args, "_external_scudo_driver_args", driver_args)
    return driver_args


def prepare_scudo_execution(
    args: argparse.Namespace,
    command: List[str],
    real_workload_dir: Path,
) -> Dict[str, Any]:
    """Resolve a real Scudo route before any child benchmark may execute."""

    driver_args = canonical_scudo_driver_args(args, command, real_workload_dir)
    probe = _paper_workload_driver.scudo_execution_probe(driver_args)
    state = "planned" if probe.get("ok") else "unavailable"
    execution = _paper_workload_driver.scudo_execution_runtime_record(
        probe,
        runtime_verified=False,
        verification_status=state,
    )
    execution["request_sources"] = list(getattr(args, "_external_scudo_request_sources", []))
    setattr(args, "_external_scudo_execution_probe", probe)
    setattr(args, "_external_scudo_execution_record", execution)
    return execution


def apply_scudo_subprocess_env(args: argparse.Namespace, env: Dict[str, str]) -> Dict[str, Any]:
    """Apply only the canonical driver's Scudo-specific child environment."""

    driver_args = getattr(args, "_external_scudo_driver_args", None)
    probe = getattr(args, "_external_scudo_execution_probe", None)
    if not isinstance(driver_args, argparse.Namespace) or not isinstance(probe, dict) or not probe.get("ok"):
        return {
            "ok": False,
            "error": "canonical Scudo execution route was not prepared",
            "scudo_execution_probe": getattr(args, "_external_scudo_execution_record", probe),
        }

    canonical_env = _paper_workload_driver.cargo_subprocess_env(driver_args)
    mode = probe.get("selected_mode")
    removed_env: List[str] = []
    if mode == "rust-sanitizer":
        removed_env = list(_paper_workload_driver.SCUDO_SANITIZER_CONFLICTING_ENV_NAMES)
        for name in removed_env:
            env.pop(name, None)
        env["RUSTFLAGS"] = _paper_workload_driver.append_env_flags(
            env.get("RUSTFLAGS"),
            _paper_workload_driver.SCUDO_SANITIZER_RUSTFLAGS,
        )
    elif mode == "ld-preload":
        runtime = probe.get("runtime_library")
        if not runtime:
            return {
                "ok": False,
                "error": "canonical LD_PRELOAD route did not identify a Scudo runtime library",
                "scudo_execution_probe": getattr(args, "_external_scudo_execution_record", probe),
            }
        runtime_path = Path(str(runtime))
        env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"] = str(runtime_path)
        env["LD_PRELOAD"] = _paper_workload_driver.prepend_preload_library(
            env.get("LD_PRELOAD"),
            runtime_path,
        )
    else:
        return {
            "ok": False,
            "error": f"unsupported canonical Scudo execution mode: {mode}",
            "scudo_execution_probe": getattr(args, "_external_scudo_execution_record", probe),
        }

    env_delta = {
        key: env[key]
        for key in ("UNIALLOC_SCUDO_RUNTIME_LIBRARY", "LD_PRELOAD", "RUSTFLAGS")
        if key in env and env.get(key) != os.environ.get(key)
    }
    # Preserve the exact canonical values as an audit cross-check.  The wrapper
    # may add legacy-toolchain RUSTFLAGS before appending the sanitizer flag.
    canonical_delta = {
        key: canonical_env[key]
        for key in ("UNIALLOC_SCUDO_RUNTIME_LIBRARY", "LD_PRELOAD", "RUSTFLAGS")
        if key in canonical_env and canonical_env.get(key) != os.environ.get(key)
    }
    record = {
        "ok": True,
        "selected_mode": mode,
        "subprocess_env_delta": env_delta,
        "subprocess_env_effective": {
            key: env[key]
            for key in ("UNIALLOC_SCUDO_RUNTIME_LIBRARY", "LD_PRELOAD", "RUSTFLAGS")
            if key in env
        },
        "canonical_subprocess_env_delta": canonical_delta,
        "scudo_execution_probe": getattr(args, "_external_scudo_execution_record", probe),
    }
    if removed_env:
        record["subprocess_env_removed"] = removed_env
        record["subprocess_env_effective_absence"] = {
            name: name not in env for name in removed_env
        }
    return record


def verify_scudo_runtime_identity(args: argparse.Namespace, stderr_text: Any) -> Dict[str, Any]:
    """Promote Scudo semantics only after the child constructor marker."""

    probe = getattr(args, "_external_scudo_execution_probe", None)
    if not isinstance(probe, dict):
        probe = {}
    verified = _paper_workload_driver.scudo_runtime_identity_marker_present(stderr_text)
    status = "runtime-verified" if verified else "verification-failed"
    execution = _paper_workload_driver.scudo_execution_runtime_record(
        probe,
        runtime_verified=verified,
        verification_status=status,
    )
    execution["request_sources"] = list(getattr(args, "_external_scudo_request_sources", []))
    setattr(args, "_external_scudo_execution_record", execution)
    return execution


def scudo_runtime_guard_source_probe(real_workload_dir: Path, command: List[str]) -> Dict[str, Any]:
    """Prove the selected bench root contains the exact current guarded overlay."""

    surface = resolve_cargo_bench_surface(real_workload_dir, command)
    overlay = allocator_bench_overlay_text()
    overlay_sha256 = hashlib.sha256(overlay.encode("utf-8")).hexdigest()
    raw_source = surface.get("bench_source")
    source_path = Path(str(raw_source)) if raw_source else None
    source_text = ""
    source_error = None
    if source_path is not None:
        try:
            source_text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            source_error = str(exc)
    exact_overlay_verified = bool(source_text) and allocator_overlay_at_crate_root(source_text, overlay)

    return {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-scudo-runtime-guard-probe",
        "ok": exact_overlay_verified,
        "cargo_bench_surface": surface,
        "guard_source": str(source_path) if source_path is not None else None,
        "guard_source_sha256": (
            hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if source_text
            else None
        ),
        "expected_overlay_sha256": overlay_sha256,
        "exact_overlay_verified": exact_overlay_verified,
        "runtime_identity_marker": SCUDO_RUNTIME_IDENTITY_MARKER,
        "error": (
            None
            if exact_overlay_verified
            else source_error
            or "selected benchmark root does not contain the exact current guarded allocator overlay"
        ),
    }


def unique_strings(values: Iterable[Any]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def slugify_label(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text).strip("-._")
    return text or fallback


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_evidence_dir(raw_dir: str, args: argparse.Namespace) -> Optional[Path]:
    raw_dir = str(raw_dir or "").strip()
    if not raw_dir:
        return None
    base = Path(raw_dir).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    label = "-".join(
        [
            slugify_label(args.dataset, "dataset"),
            slugify_label(args.benchmark, "benchmark"),
            slugify_label(args.allocator, "allocator"),
            f"run{slugify_label(args.run_index, '0')}",
            str(os.getpid()),
            str(int(time.time() * 1000)),
        ]
    )
    evidence_dir = base / label
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return evidence_dir


def write_evidence_file(evidence_dir: Optional[Path], name: str, data: bytes, kind: str) -> Optional[Dict[str, Any]]:
    if evidence_dir is None:
        return None
    path = evidence_dir / slugify_label(name, "evidence")
    path.write_bytes(data)
    return {
        "kind": kind,
        "path": str(path),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def append_evidence(record: Dict[str, Any], entries: Iterable[Optional[Dict[str, Any]]]) -> None:
    current = record.get("evidence")
    if not isinstance(current, list):
        current = []
    seen = {
        str(entry.get("path"))
        for entry in current
        if isinstance(entry, dict) and str(entry.get("path") or "").strip()
    }
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        current.append(entry)
    if current:
        record["evidence"] = current
        record["raw_evidence"] = current
        first = current[0]
        if isinstance(first, dict):
            record.setdefault("raw_evidence_path_or_uri", first.get("path"))
            record.setdefault("raw_evidence_sha256", first.get("sha256"))


def csv_values(value: Any) -> List[str]:
    items: List[str] = []
    for part in str(value or "").split(","):
        text = part.strip()
        if text:
            items.append(text)
    return unique_strings(items)


def claim_grade_blocker_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def json_arg_object(value: Any) -> Dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"_parse_error": "invalid JSON"}
    return parsed if isinstance(parsed, dict) else {"_parse_error": "JSON value is not an object"}


def evidence_contract_blockers(record: Dict[str, Any]) -> List[str]:
    evidence = record.get("evidence") if isinstance(record.get("evidence"), list) else []
    blockers: List[str] = []
    if not evidence:
        return ["raw stdout/stderr evidence is missing"]
    for entry in evidence:
        if not isinstance(entry, dict):
            blockers.append("raw evidence entry is not an object")
            continue
        path_text = str(entry.get("path") or "").strip()
        digest = str(entry.get("sha256") or "").strip().lower()
        if not path_text:
            blockers.append("raw evidence entry is missing path")
            continue
        path = Path(path_text).expanduser()
        if not path.exists():
            blockers.append(f"raw evidence path does not exist: {path_text}")
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            blockers.append(f"raw evidence entry lacks sha256 digest: {path_text}")
            continue
        actual = file_sha256(path)
        if actual.lower() != digest:
            blockers.append(f"raw evidence sha256 mismatch: {path_text}")
    return unique_strings(blockers)


def source_contract_blockers(contract: Dict[str, Any]) -> List[str]:
    if not contract:
        return ["paper source contract is missing"]
    if contract.get("_parse_error"):
        return [f"paper source contract is invalid: {contract.get('_parse_error')}"]
    blockers: List[str] = []
    if contract.get("paper_exact_ref_complete") is not True:
        blockers.append("paper source contract is not paper_exact_ref_complete=true")
    if contract.get("claim_grade_complete") is not True:
        blockers.append("paper source contract is not claim_grade_complete=true")
    if contract.get("exact_checkout_pin_complete") is not True:
        blockers.append("paper source contract is not exact_checkout_pin_complete=true")
    checkout_pin = contract.get("checkout_pin") if isinstance(contract.get("checkout_pin"), dict) else {}
    if checkout_pin.get("pinned") is not True or checkout_pin.get("exact_pinned") is not True:
        blockers.append("paper source checkout pin is not exact/pinned")
    nested = contract.get("blockers")
    if isinstance(nested, list):
        blockers.extend(f"paper source contract blocker: {item}" for item in nested if str(item).strip())
    elif nested:
        blockers.append(f"paper source contract blocker: {nested}")
    return unique_strings(blockers)


def rpolars_dependency_contract_blockers(record: Dict[str, Any]) -> List[str]:
    """Return R-Polars dependency/input blockers for claim-grade timing.

    The R-Polars bridge can repair historical dependency drift and can generate
    a small compatibility CSV so the upstream `groupby` Criterion target is
    runnable.  Those repairs are useful real smoke evidence, but they are not
    automatically paper input provenance.  Keep that distinction in the wrapper
    contract so a successful multi-target run cannot be promoted simply because
    it produced finite timings.
    """

    if str(record.get("benchmark") or "") != "R-Polars":
        return []
    dependency_env = record.get("dependency_env") if isinstance(record.get("dependency_env"), dict) else {}
    fixture = dependency_env.get("polars_csv_fixture") if isinstance(dependency_env.get("polars_csv_fixture"), dict) else {}
    blockers: List[str] = []
    if fixture and fixture.get("claim_grade") is not True:
        reason = str(fixture.get("not_claim_grade_reason") or "").strip()
        blockers.append(
            reason
            or "R-Polars CSV fixture does not prove paper-equivalent raw input provenance"
        )
    return unique_strings(blockers)


def rpolars_input_contract_blockers(record: Dict[str, Any], contract: Dict[str, Any]) -> List[str]:
    """Require plan-generated R-Polars input provenance for claim-grade runs."""

    if str(record.get("benchmark") or "") != "R-Polars":
        return []
    return rpolars_validate_input_contract_against_csv(
        contract,
        rpolars_actual_csv_src(record),
    )


def apply_success_claim_grade_contract(
    record: Dict[str, Any],
    args: argparse.Namespace,
    *,
    requested_targets: List[str],
) -> None:
    """Attach a strict claim-grade contract to a successful cargo-bench record.

    The default wrapper remains diagnostic-only.  Generated paper-equivalent
    plans must explicitly pass expected target/leaf surfaces and an exact paper
    source contract before this bridge can emit claim_grade=true.  Fragment or
    run-level claim_grade=true means "this selected cargo-bench record is valid
    evidence"; the downstream claim-matrix audits still require the union of all
    surfaces and repetitions before a paper cell is usable.
    """

    mode = str(getattr(args, "claim_grade_contract", "") or "off").strip()
    allocator_semantics = record.get("allocator_semantics") if isinstance(record.get("allocator_semantics"), dict) else {}
    contract: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-claim-grade-contract",
        "mode": mode,
        "requested_targets": list(requested_targets),
        "bench_name_filter": getattr(args, "bench_name_filter", "") or None,
        "expected_cargo_bench_targets": csv_values(getattr(args, "expected_cargo_bench_targets", "")),
        "expected_libtest_bench_functions": csv_values(getattr(args, "expected_libtest_bench_functions", "")),
        "observed_libtest_bench_functions": unique_strings(row.get("name") for row in record.get("bench_rows", []) if isinstance(row, dict)),
    }
    source_contract = json_arg_object(getattr(args, "paper_source_contract_json", ""))
    paper_source = json_arg_object(getattr(args, "paper_source_json", ""))
    rpolars_input_contract = json_arg_object(getattr(args, "rpolars_input_contract_json", ""))
    if source_contract:
        record["source_contract"] = source_contract
        contract["source_contract"] = source_contract
    if paper_source:
        record["paper_source"] = paper_source
    if rpolars_input_contract:
        record["rpolars_input_provenance"] = rpolars_input_contract
        contract["rpolars_input_provenance"] = rpolars_input_contract
    blockers: List[str] = []
    if mode in {"", "off", "none", "diagnostic"}:
        blockers.append("cargo-bench JSON wrapper is an adapter bridge only; claim-grade contract was not requested")
    elif mode not in {"roxipng-paper-fragment", "rpolars-paper-run", "rjs-compiler-paper-run", "rpython-paper-run"}:
        blockers.append(f"unsupported claim-grade contract mode: {mode}")
    elif mode == "roxipng-paper-fragment":
        expected_targets = set(contract["expected_cargo_bench_targets"])
        expected_leafs = set(contract["expected_libtest_bench_functions"])
        observed_leafs = set(contract["observed_libtest_bench_functions"])
        selected_targets = set(requested_targets)
        if str(record.get("benchmark") or "") != "R-Oxipng*":
            blockers.append("R-Oxipng paper-fragment contract used for a non R-Oxipng* benchmark")
        if not expected_targets:
            blockers.append("expected cargo bench target surface is missing")
        if not selected_targets:
            blockers.append("selected cargo bench target is missing")
        if expected_targets and selected_targets and not selected_targets.issubset(expected_targets):
            blockers.append("selected cargo bench target is outside the expected R-Oxipng surface")
        if not expected_leafs:
            blockers.append("expected libtest bench function surface is missing")
        if expected_leafs and not expected_leafs.issubset(observed_leafs):
            missing = sorted(expected_leafs - observed_leafs)
            blockers.append(
                "observed libtest surface is missing expected function(s): "
                + ", ".join(missing[:12])
                + (" ..." if len(missing) > 12 else "")
            )
        bench_filter = str(getattr(args, "bench_name_filter", "") or "").strip()
        if bench_filter and expected_leafs != {bench_filter}:
            blockers.append("bench-name filter is only claim-grade for a declared single-leaf fragment")
        if allocator_semantics.get("paper_allocator_equivalent") is not True:
            blockers.append("allocator semantics are not paper-equivalent for this host/selector")
        blockers.extend(str(item) for item in allocator_semantics.get("claim_grade_blockers", []) if str(item).strip())
        dependency_env = record.get("dependency_env") if isinstance(record.get("dependency_env"), dict) else {}
        if dependency_env.get("cargo_network_allowed") is True:
            blockers.append("cargo network was allowed during claim-grade cargo bench")
        if record.get("stdout_truncated") or record.get("stderr_truncated"):
            blockers.append("raw child output was truncated")
        blockers.extend(evidence_contract_blockers(record))
        blockers.extend(source_contract_blockers(source_contract))
    elif mode == "rpolars-paper-run":
        expected_targets = set(contract["expected_cargo_bench_targets"])
        expected_leafs = set(contract["expected_libtest_bench_functions"])
        observed_leafs = set(contract["observed_libtest_bench_functions"])
        selected_targets = set(requested_targets)
        if str(record.get("benchmark") or "") != "R-Polars":
            blockers.append("R-Polars paper-run contract used for a non R-Polars benchmark")
        if not expected_targets:
            blockers.append("expected R-Polars cargo bench target surface is missing")
        elif not RPOLARS_PAPER_CARGO_BENCH_TARGETS.issubset(expected_targets):
            missing = sorted(RPOLARS_PAPER_CARGO_BENCH_TARGETS - expected_targets)
            blockers.append(
                "expected R-Polars cargo bench target surface is not the canonical paper surface; missing "
                + ", ".join(missing)
            )
        if not selected_targets:
            blockers.append("selected R-Polars cargo bench target surface is missing")
        if expected_targets and selected_targets and selected_targets != expected_targets:
            missing = sorted(expected_targets - selected_targets)
            extra = sorted(selected_targets - expected_targets)
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else ""))
            if extra:
                detail.append("extra " + ", ".join(extra[:12]) + (" ..." if len(extra) > 12 else ""))
            blockers.append(
                "selected R-Polars cargo bench targets do not exactly match the expected paper surface"
                + (": " + "; ".join(detail) if detail else "")
            )
        if expected_leafs and not expected_leafs.issubset(observed_leafs):
            missing = sorted(expected_leafs - observed_leafs)
            blockers.append(
                "observed R-Polars libtest surface is missing expected function(s): "
                + ", ".join(missing[:12])
                + (" ..." if len(missing) > 12 else "")
            )
        bench_filter = str(getattr(args, "bench_name_filter", "") or "").strip()
        if bench_filter:
            blockers.append("bench-name filters are not claim-grade for a full R-Polars paper run")
        if allocator_semantics.get("paper_allocator_equivalent") is not True:
            blockers.append("allocator semantics are not paper-equivalent for this host/selector")
        blockers.extend(str(item) for item in allocator_semantics.get("claim_grade_blockers", []) if str(item).strip())
        dependency_env = record.get("dependency_env") if isinstance(record.get("dependency_env"), dict) else {}
        if dependency_env.get("cargo_network_allowed") is True:
            blockers.append("cargo network was allowed during claim-grade cargo bench")
        blockers.extend(rpolars_dependency_contract_blockers(record))
        blockers.extend(rpolars_input_contract_blockers(record, rpolars_input_contract))
        blockers.extend(rpolars_cargo_bench_command_blockers(record, args, requested_targets=requested_targets))
        if record.get("stdout_truncated") or record.get("stderr_truncated"):
            blockers.append("raw child output was truncated")
        blockers.extend(evidence_contract_blockers(record))
        blockers.extend(source_contract_blockers(source_contract))
    elif mode == "rjs-compiler-paper-run":
        expected_targets = set(contract["expected_cargo_bench_targets"])
        expected_leafs = set(contract["expected_libtest_bench_functions"])
        observed_leafs = set(contract["observed_libtest_bench_functions"])
        selected_targets = set(requested_targets)
        if str(record.get("benchmark") or "") != "RJS-Compiler":
            blockers.append("RJS-Compiler paper-run contract used for a non RJS-Compiler benchmark")
        if not expected_targets:
            blockers.append("expected RJS-Compiler cargo bench target surface is missing")
        elif expected_targets != {"typescript"}:
            blockers.append(
                "expected RJS-Compiler cargo bench target surface must be exactly the SWC top-level typescript bench"
            )
        if not selected_targets:
            blockers.append("selected RJS-Compiler cargo bench target surface is missing")
        if expected_targets and selected_targets and selected_targets != expected_targets:
            missing = sorted(expected_targets - selected_targets)
            extra = sorted(selected_targets - expected_targets)
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("extra " + ", ".join(extra))
            blockers.append(
                "selected RJS-Compiler cargo bench targets do not exactly match the expected paper surface"
                + (": " + "; ".join(detail) if detail else "")
            )
        if not expected_leafs:
            blockers.append("expected RJS-Compiler libtest function surface is missing")
        elif not expected_leafs.issubset(observed_leafs):
            missing = sorted(expected_leafs - observed_leafs)
            blockers.append(
                "observed RJS-Compiler libtest surface is missing expected function(s): "
                + ", ".join(missing[:12])
                + (" ..." if len(missing) > 12 else "")
            )
        bench_filter = str(getattr(args, "bench_name_filter", "") or "").strip()
        if bench_filter:
            blockers.append("bench-name filters are not claim-grade for a full RJS-Compiler paper run")
        if allocator_semantics.get("paper_allocator_equivalent") is not True:
            blockers.append("allocator semantics are not paper-equivalent for this host/selector")
        blockers.extend(str(item) for item in allocator_semantics.get("claim_grade_blockers", []) if str(item).strip())
        dependency_env = record.get("dependency_env") if isinstance(record.get("dependency_env"), dict) else {}
        if dependency_env.get("cargo_network_allowed") is True:
            blockers.append("cargo network was allowed during claim-grade cargo bench")
        if record.get("stdout_truncated") or record.get("stderr_truncated"):
            blockers.append("raw child output was truncated")
        blockers.extend(evidence_contract_blockers(record))
        blockers.extend(source_contract_blockers(source_contract))
    else:
        expected_targets = set(contract["expected_cargo_bench_targets"])
        expected_leafs = set(contract["expected_libtest_bench_functions"])
        observed_leafs = set(contract["observed_libtest_bench_functions"])
        selected_targets = set(requested_targets)
        if str(record.get("benchmark") or "") != "RPython":
            blockers.append("RPython paper-run contract used for a non RPython benchmark")
        if not expected_targets:
            blockers.append("expected RPython cargo bench target surface is missing")
        if not selected_targets:
            blockers.append("selected RPython cargo bench target surface is missing")
        if expected_targets and selected_targets and selected_targets != expected_targets:
            missing = sorted(expected_targets - selected_targets)
            extra = sorted(selected_targets - expected_targets)
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("extra " + ", ".join(extra))
            blockers.append(
                "selected RPython cargo bench targets do not exactly match the expected paper surface"
                + (": " + "; ".join(detail) if detail else "")
            )
        if not expected_leafs and expected_targets == {"bench"}:
            blockers.append("expected RPython libtest function surface is missing")
        elif not expected_leafs.issubset(observed_leafs):
            missing = sorted(expected_leafs - observed_leafs)
            blockers.append(
                "observed RPython libtest surface is missing expected function(s): "
                + ", ".join(missing[:12])
                + (" ..." if len(missing) > 12 else "")
            )
        bench_filter = str(getattr(args, "bench_name_filter", "") or "").strip()
        if bench_filter:
            blockers.append("bench-name filters are not claim-grade for a full RPython paper run")
        if allocator_semantics.get("paper_allocator_equivalent") is not True:
            blockers.append("allocator semantics are not paper-equivalent for this host/selector")
        blockers.extend(str(item) for item in allocator_semantics.get("claim_grade_blockers", []) if str(item).strip())
        dependency_env = record.get("dependency_env") if isinstance(record.get("dependency_env"), dict) else {}
        if dependency_env.get("cargo_network_allowed") is True:
            blockers.append("cargo network was allowed during claim-grade cargo bench")
        if record.get("stdout_truncated") or record.get("stderr_truncated"):
            blockers.append("raw child output was truncated")
        blockers.extend(evidence_contract_blockers(record))
        blockers.extend(source_contract_blockers(source_contract))
    blockers = unique_strings(blockers)
    contract["blockers"] = blockers
    contract["claim_grade_ready"] = not blockers
    record["claim_grade_contract"] = contract
    scope_by_mode = {
        "rpolars-paper-run": "rpolars-paper-equivalent-matrix-run",
        "roxipng-paper-fragment": "roxipng-paper-equivalent-matrix-fragment",
        "rjs-compiler-paper-run": "rjs-compiler-paper-equivalent-matrix-run",
        "rpython-paper-run": "rpython-paper-equivalent-matrix-run",
    }
    record["claim_grade_scope"] = scope_by_mode.get(mode, "cargo-bench-json-diagnostic")
    if blockers:
        record["claim_grade"] = False
        record["claim_grade_blockers"] = unique_strings(
            [*claim_grade_blocker_values(record.get("claim_grade_blockers")), *blockers]
        )
    else:
        record["claim_grade"] = True
        record["claim_grade_blockers"] = []


def allocator_semantics_record(args: argparse.Namespace) -> Dict[str, Any]:
    """Describe whether the selected feature matches the named paper allocator.

    The cargo-bench bridge patches historical benchmark crates with feature
    gated global allocators.  `bench_ptmalloc` resolves through the host System
    allocator.  `bench_scudo` also uses Rust's System ABI surface, while a
    fail-closed constructor plus the canonical workload-driver route proves
    that malloc/free resolve to the selected Scudo runtime before timings can be
    accepted.
    """

    allocator = normalize_allocator_name(args.allocator)
    feature = allocator_feature(allocator)
    system = platform.system()
    libc_name, libc_version = platform.libc_ver()
    blockers: List[str] = []
    implementation_kind = "allocator_feature_global_allocator"
    paper_allocator_equivalent: Optional[bool] = None
    notes: List[str] = []

    if feature == "bench_ptmalloc":
        implementation_kind = "std_alloc_system_fallback"
        is_linux_glibc = system == "Linux" and libc_name.lower() == "glibc"
        paper_allocator_equivalent = is_linux_glibc and allocator == "ptmalloc"
        if is_linux_glibc and allocator == "ptmalloc":
            notes.append("bench_ptmalloc routes through std::alloc::System on a Linux/glibc host")
        else:
            blockers.append(
                "bench_ptmalloc routes to std::alloc::System on this host, not Linux/glibc ptmalloc paper evidence"
            )
    elif feature == "bench_scudo":
        execution = getattr(args, "_external_scudo_execution_record", None)
        execution = execution if isinstance(execution, dict) else {}
        runtime_verified = execution.get("runtime_verified") is True
        status = str(execution.get("execution_state") or "unresolved")
        implementation_kind = (
            "verified_external_scudo_runtime"
            if runtime_verified
            else "planned_scudo_runtime_route"
            if status == "planned"
            else "unavailable_scudo_runtime_route"
            if status == "unavailable"
            else "unverified_scudo_runtime_route"
        )
        paper_allocator_equivalent = runtime_verified
        if runtime_verified:
            notes.append(
                "bench_scudo malloc/free providers matched the Scudo identity-symbol provider and emitted the required runtime marker"
            )
        elif status == "planned":
            blockers.append(
                "Scudo execution route is planned; the child runtime identity marker has not been verified"
            )
        elif status == "unavailable":
            blockers.extend(str(item) for item in execution.get("blockers", []) if str(item).strip())
            blockers.append("no executable Scudo runtime route is available")
        else:
            blockers.append("Scudo runtime identity was not verified by the child benchmark")
    elif feature:
        paper_allocator_equivalent = True

    record = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-allocator-semantics",
        "requested_allocator": allocator or None,
        "allocator_feature": feature or None,
        "implementation_kind": implementation_kind,
        "host_system": system,
        "host_libc": libc_name or None,
        "host_libc_version": libc_version or None,
        "paper_allocator_equivalent": paper_allocator_equivalent,
        "claim_grade_blockers": blockers,
        "notes": notes,
    }
    if feature == "bench_scudo":
        execution = getattr(args, "_external_scudo_execution_record", None)
        if isinstance(execution, dict):
            canonical = _paper_workload_driver.scudo_allocator_semantics(
                execution,
                runtime_verified=execution.get("runtime_verified") is True,
                verification_status=str(execution.get("execution_state") or "unresolved"),
            )
            record.update(
                {
                    "runtime_identity_verified": canonical.get("runtime_identity_verified"),
                    "runtime_authenticity_verified": canonical.get("runtime_authenticity_verified"),
                    "runtime_identity_status": canonical.get("runtime_identity_status"),
                    "runtime_identity_marker": canonical.get("runtime_identity_marker"),
                    "scudo_runtime_mode": canonical.get("scudo_runtime_mode"),
                    "scudo_runtime_library": canonical.get("scudo_runtime_library"),
                    "scudo_runtime_library_identity": canonical.get("scudo_runtime_library_identity"),
                    "scudo_runtime_authenticity": canonical.get("scudo_runtime_authenticity"),
                    "runtime_provenance": canonical.get("runtime_provenance"),
                }
            )
    return record


def section_bounds(text: str, header: str) -> Optional[tuple[int, int]]:
    start = text.find(header)
    if start < 0:
        return None
    next_section = re.search(r"(?m)^\[", text[start + len(header) :])
    end = len(text) if next_section is None else start + len(header) + next_section.start()
    return start, end


def cargo_toml_has_dependency(text: str, name: str) -> bool:
    return (
        f"[dependencies.{name}]" in text
        or f"[dev-dependencies.{name}]" in text
        or re.search(rf"(?m)^\s*{re.escape(name)}\s*=", text) is not None
    )


def ensure_dependency_table_block(text: str, name: str, block: str) -> tuple[str, bool]:
    wanted = block.rstrip() + "\n"
    if wanted in text:
        return text, False
    bounds = section_bounds(text, f"[dependencies.{name}]")
    if bounds is None:
        return text.rstrip() + "\n\n" + wanted, True
    start, end = bounds
    return text[:start] + wanted + text[end:].lstrip("\n"), True


def remove_exact_dependency_table_block(text: str, name: str, block: str) -> tuple[str, bool]:
    bounds = section_bounds(text, f"[dependencies.{name}]")
    if bounds is None:
        return text, False
    start, end = bounds
    current = text[start:end].strip()
    wanted = block.strip()
    if current != wanted:
        return text, False
    return (text[:start].rstrip() + "\n\n" + text[end:].lstrip("\n")).rstrip() + "\n", True


def old_nightly_pin_blocks_for_feature(feature: str) -> Dict[str, str]:
    blocks = {
        "bench_ourself": {
            "lock_api": '[dependencies.lock_api]\nversion = "=0.4.3"\n',
            "scopeguard": '[dependencies.scopeguard]\nversion = "=1.1.0"\n',
            "num_cpus": '[dependencies.num_cpus]\nversion = "=1.13.0"\n',
            "memchr": '[dependencies.memchr]\nversion = "=2.4.1"\n',
            "cc": '[dependencies.cc]\nversion = "=1.0.72"\n',
            "uuid": '[dependencies.uuid]\nversion = "=1.0.0"\n',
            "anyhow": '[dependencies.anyhow]\nversion = "=1.0.66"\n',
            "csv": '[dependencies.csv]\nversion = "=1.1.6"\n',
            "itoa": '[dependencies.itoa]\nversion = "=1.0.6"\n',
            "ryu": '[dependencies.ryu]\nversion = "=1.0.12"\n',
            "serde_json": '[dependencies.serde_json]\nversion = "=1.0.85"\n',
            "syn": '[dependencies.syn]\nversion = "=2.0.52"\n',
            "quote": '[dependencies.quote]\nversion = "=1.0.35"\n',
            "proc-macro2": '[dependencies.proc-macro2]\nversion = "=1.0.78"\n',
            "unicode-ident": '[dependencies.unicode-ident]\nversion = "=1.0.12"\n',
            "unicode-width": '[dependencies.unicode-width]\nversion = "=0.1.10"\n',
            "once_cell": '[dependencies.once_cell]\nversion = "=1.17.2"\n',
            # R-Oxipng exposes rayon through `parallel = ["rayon", ...]`.
            # Cargo requires feature-addressed dependencies to be optional, so
            # preserve that manifest contract while still pinning a
            # paper-toolchain-compatible rayon release.
            "rayon": '[dependencies.rayon]\noptional = true\nversion = "=1.5.3"\n',
            "rayon-core": '[dependencies.rayon-core]\nversion = "=1.9.3"\n',
        },
        "bench_snmalloc": {
            "cmake": '[dependencies.cmake]\nversion = "=0.1.49"\n',
        },
    }
    return blocks.get(feature, {})


def cargo_lockfile_version(lockfile: Path) -> Optional[int]:
    try:
        text = lockfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*version\s*=\s*(\d+)\s*$", text)
    return int(match.group(1)) if match else None


def repair_known_workspace_dependency_drift(
    real_workload_dir: Path,
    *,
    effective_toolchain: str = "",
    mutation_journal: Optional[FileMutationJournal] = None,
) -> Dict[str, Any]:
    """Repair source-era dependency pointers that no longer resolve.

    The wrapper remains non-claim-grade after this repair: the purpose is to
    move diagnostic runs past known dependency-resolution drift so later
    failures or timings reflect the external workload surface itself.  Keep the
    edits exact and workload-specific to avoid masking unrelated Cargo changes.
    """

    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-known-dependency-drift-repair",
        "real_workload_dir": str(real_workload_dir),
        "effective_toolchain": effective_toolchain or None,
        "prepared": True,
        "changed": False,
        "repairs": [],
        "claim_grade": False,
        "not_claim_grade_reason": (
            "dependency-drift adapter repair only; paper claim evidence still requires "
            "full workload parity, allocator matrix coverage, repetitions, and raw provenance"
        ),
    }

    polars_core_manifest = real_workload_dir / "polars" / "polars-core" / "Cargo.toml"
    if not polars_core_manifest.exists():
        record["repairs"].append(
            {
                "workload": "R-Polars",
                "manifest": str(polars_core_manifest),
                "changed": False,
                "reason": "manifest not present",
            }
        )
    else:
        text = polars_core_manifest.read_text(encoding="utf-8")
        if POLARS_JSONPATH_STALE_GIT_DEP in text:
            next_text = text.replace(POLARS_JSONPATH_STALE_GIT_DEP, POLARS_JSONPATH_CRATES_IO_DEP, 1)
            if mutation_journal is not None:
                mutation_journal.snapshot(polars_core_manifest)
            polars_core_manifest.write_text(next_text, encoding="utf-8")
            record["changed"] = True
            record["repairs"].append(
                {
                    "workload": "R-Polars",
                    "manifest": str(polars_core_manifest),
                    "dependency": "jsonpath_lib",
                    "changed": True,
                    "from": "stale git branch https://github.com/ritchie46/jsonpath improve_compiled",
                    "to": "crates.io jsonpath_lib 0.3.0",
                    "reason": "paper-era git branch no longer resolves, while jsonpath_lib 0.3.0 is available as a registry crate",
                }
            )
        elif POLARS_JSONPATH_CRATES_IO_DEP in text:
            record["repairs"].append(
                {
                    "workload": "R-Polars",
                    "manifest": str(polars_core_manifest),
                    "dependency": "jsonpath_lib",
                    "changed": False,
                    "reason": "registry dependency already present",
                }
            )
        else:
            record["repairs"].append(
                {
                    "workload": "R-Polars",
                    "manifest": str(polars_core_manifest),
                    "dependency": "jsonpath_lib",
                    "changed": False,
                    "reason": "expected stale dependency line not present; left manifest unchanged",
                }
            )

    lockfile = real_workload_dir / "Cargo.lock"
    lock_version = cargo_lockfile_version(lockfile) if lockfile.exists() else None
    old_cargo_toolchain = any(token in effective_toolchain for token in ("nightly-2021", "nightly-2022"))
    if lock_version and lock_version > 3 and old_cargo_toolchain:
        if mutation_journal is not None:
            mutation_journal.snapshot(lockfile)
        lockfile.unlink()
        record["changed"] = True
        record["repairs"].append(
            {
                "workload": "R-Polars",
                "manifest": str(lockfile),
                "dependency": "Cargo.lock",
                "changed": True,
                "from": f"lockfile version {lock_version}",
                "to": "removed so the selected historical cargo can resolve a readable lockfile",
                "reason": "newer cargo generated a lockfile that the selected paper-compatible toolchain cannot parse",
            }
        )
    elif lockfile.exists():
        record["repairs"].append(
            {
                "workload": "R-Polars",
                "manifest": str(lockfile),
                "dependency": "Cargo.lock",
                "changed": False,
                "lockfile_version": lock_version,
                "reason": "lockfile is absent, readable, or the selected toolchain is not a known historical cargo",
            }
        )
    return record


def effective_rust_toolchain_for_workload(real_workload_dir: Path, command: Optional[List[str]] = None) -> str:
    explicit_toolchain = ""
    if command:
        cargo_index = next((index for index, token in enumerate(command) if Path(token).name == "cargo"), -1)
        if cargo_index >= 0 and cargo_index + 1 < len(command) and command[cargo_index + 1].startswith("+"):
            explicit_toolchain = command[cargo_index + 1][1:]
    return explicit_toolchain or os.environ.get("RUSTUP_TOOLCHAIN", "") or read_local_rust_toolchain(real_workload_dir)


def ensure_feature_line(text: str, name: str, deps: List[str]) -> tuple[str, bool]:
    line = f'{name} = [{", ".join(json.dumps(dep) for dep in deps)}]'
    if re.search(rf"(?m)^\s*{re.escape(name)}\s*=", text):
        return text, False
    bounds = section_bounds(text, "[features]")
    if bounds is None:
        addition = "\n[features]\n" + line + "\n"
        return text.rstrip() + "\n" + addition, True
    start, _end = bounds
    insert_at = start + len("[features]")
    return text[:insert_at] + "\n" + line + text[insert_at:], True


def selected_package_name(command: List[str]) -> str:
    for index, token in enumerate(command):
        if token in {"--package", "-p"} and index + 1 < len(command):
            return str(command[index + 1]).strip()
        for prefix in ("--package=", "-p="):
            if token.startswith(prefix):
                return token[len(prefix) :].strip()
    return ""


def selected_manifest_path(command: List[str], real_workload_dir: Path) -> Optional[Path]:
    for index, token in enumerate(command):
        if token == "--manifest-path" and index + 1 < len(command):
            raw = Path(str(command[index + 1]).strip())
            return raw if raw.is_absolute() else real_workload_dir / raw
        prefix = "--manifest-path="
        if token.startswith(prefix):
            raw = Path(token[len(prefix) :].strip())
            return raw if raw.is_absolute() else real_workload_dir / raw
    return None


def package_name_from_manifest(manifest: Path) -> str:
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return ""
    bounds = section_bounds(text, "[package]")
    if bounds is None:
        return ""
    start, end = bounds
    match = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$', text[start:end])
    return match.group(1).strip() if match else ""


def find_package_manifest(real_workload_dir: Path, package: str) -> Optional[Path]:
    package = package.strip()
    if not package:
        root_manifest = real_workload_dir / "Cargo.toml"
        return root_manifest if root_manifest.exists() else None
    root_manifest = real_workload_dir / "Cargo.toml"
    if root_manifest.exists() and package_name_from_manifest(root_manifest) == package:
        return root_manifest
    ignored_parts = {".git", "target"}
    manifests = sorted(
        real_workload_dir.rglob("Cargo.toml"),
        key=lambda path: (path.parent.name != package, len(path.parts), str(path)),
    )
    for manifest in manifests:
        try:
            rel_parts = set(manifest.relative_to(real_workload_dir).parts)
        except ValueError:
            rel_parts = set(manifest.parts)
        if ignored_parts & rel_parts:
            continue
        if package_name_from_manifest(manifest) == package:
            return manifest
    return None


def bench_path_from_manifest(manifest: Path, bench_name: str) -> Optional[Path]:
    if not bench_name:
        return None
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    bench_block_pattern = re.compile(
        r"(?ms)^\s*\[\[bench\]\]\s*$"
        r"(?P<body>.*?)(?=^\s*\[\[|^\s*\[[^\[]|\Z)"
    )
    for match in bench_block_pattern.finditer(text):
        body = match.group("body")
        name_match = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$', body)
        if not name_match or name_match.group(1).strip() != bench_name:
            continue
        path_match = re.search(r'(?m)^\s*path\s*=\s*"([^"]+)"\s*$', body)
        if path_match:
            return (manifest.parent / path_match.group(1).strip()).resolve()
        return (manifest.parent / "benches" / f"{bench_name}.rs").resolve()
    fallback = (manifest.parent / "benches" / f"{bench_name}.rs").resolve()
    return fallback if fallback.exists() else None


def resolve_cargo_bench_surface(real_workload_dir: Path, command: List[str]) -> Dict[str, Any]:
    bench_name = selected_bench_name(command)
    package = selected_package_name(command)
    manifest_path = selected_manifest_path(command, real_workload_dir)
    manifest_source = "command_manifest_path" if manifest_path else "package_lookup"
    if manifest_path is None:
        manifest_path = find_package_manifest(real_workload_dir, package)
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-surface",
        "real_workload_dir": str(real_workload_dir),
        "package": package or None,
        "bench_name": bench_name or None,
        "package_manifest": str(manifest_path) if manifest_path else None,
        "package_manifest_source": manifest_source,
        "bench_source": None,
        "resolved": False,
    }
    if not bench_name:
        record["error"] = "cargo bench command is missing --bench"
        return record
    if manifest_path is None or not manifest_path.exists():
        record["error"] = f"could not resolve Cargo.toml for package {package!r}"
        return record
    manifest_path = manifest_path.resolve()
    manifest_package = package_name_from_manifest(manifest_path)
    bench_source = bench_path_from_manifest(manifest_path, bench_name)
    record.update(
        {
            "package_manifest": str(manifest_path),
            "manifest_package": manifest_package or None,
            "bench_source": str(bench_source) if bench_source else None,
        }
    )
    if bench_source is None:
        record["error"] = f"could not resolve benchmark source for bench {bench_name!r} in {manifest_path}"
        return record
    record["resolved"] = True
    return record


def cargo_bench_command_blockers(command: List[str], real_workload_dir: Path) -> List[str]:
    """Reject claim-grade R-Polars evidence unless the measured child is cargo bench."""

    blockers: List[str] = []
    cargo_index = next(
        (index for index, token in enumerate(command) if Path(str(token)).name == "cargo"),
        -1,
    )
    if cargo_index < 0:
        return ["claim-grade R-Polars child command is not cargo bench: no cargo executable token"]
    try:
        separator = command.index("--", cargo_index + 1)
    except ValueError:
        separator = len(command)
    cargo_args = command[cargo_index + 1 : separator]
    if "bench" not in cargo_args:
        blockers.append("claim-grade R-Polars child command is not cargo bench: missing bench subcommand")
    bench_name = selected_bench_name(command)
    if bench_name not in RPOLARS_PAPER_CARGO_BENCH_TARGETS:
        blockers.append(
            "claim-grade R-Polars child command selects a non-canonical bench target"
            f": {bench_name or '<missing>'}"
        )
    if not real_workload_dir.exists():
        blockers.append(f"claim-grade R-Polars real workload directory does not exist: {real_workload_dir}")
        return unique_strings(blockers)
    surface = resolve_cargo_bench_surface(real_workload_dir, command)
    if surface.get("resolved") is not True:
        blockers.append(
            "claim-grade R-Polars cargo bench surface could not be resolved"
            + (f": {surface.get('error')}" if surface.get("error") else "")
        )
        return unique_strings(blockers)
    manifest_package = str(surface.get("manifest_package") or surface.get("package") or "").strip()
    if manifest_package != "polars":
        blockers.append(
            "claim-grade R-Polars cargo bench package is not polars"
            f": {manifest_package or '<missing>'}"
        )
    bench_source_text = str(surface.get("bench_source") or "").strip()
    if not bench_source_text:
        blockers.append("claim-grade R-Polars cargo bench source path is missing")
    else:
        try:
            bench_source = Path(bench_source_text).resolve()
            bench_source.relative_to(real_workload_dir.resolve())
        except (OSError, ValueError):
            blockers.append(
                "claim-grade R-Polars cargo bench source is not inside the real workload checkout"
                f": {bench_source_text}"
            )
    return unique_strings(blockers)


def rpolars_cargo_bench_command_blockers(
    record: Dict[str, Any],
    args: argparse.Namespace,
    *,
    requested_targets: List[str],
) -> List[str]:
    """Validate every target command in a multi-target R-Polars record."""

    if str(record.get("benchmark") or "") != "R-Polars":
        return []
    real_workload_dir = Path(args.real_workload_dir).expanduser().resolve() if args.real_workload_dir else Path.cwd().resolve()
    target_records = record.get("target_records") if isinstance(record.get("target_records"), list) else []
    command_records = [item for item in target_records if isinstance(item, dict)] or [{"command": record.get("command")}]
    blockers: List[str] = []
    observed_targets: List[str] = []
    for item in command_records:
        command = [str(part) for part in item.get("command", [])] if isinstance(item.get("command"), list) else []
        target = str(item.get("cargo_bench_target") or selected_bench_name(command) or "").strip()
        if target:
            observed_targets.append(target)
        if not command:
            blockers.append("claim-grade R-Polars target record lacks the measured child command")
            continue
        blockers.extend(cargo_bench_command_blockers(command, real_workload_dir))
    expected_targets = set(requested_targets)
    observed_target_set = set(observed_targets)
    if expected_targets and observed_target_set != expected_targets:
        missing = sorted(expected_targets - observed_target_set)
        extra = sorted(observed_target_set - expected_targets)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("extra " + ", ".join(extra))
        blockers.append(
            "claim-grade R-Polars target_records do not match requested cargo bench targets"
            + (": " + "; ".join(detail) if detail else "")
        )
    return unique_strings(blockers)


def ensure_allocator_cargo_overlay(
    real_workload_dir: Path,
    command: List[str],
    surface: Optional[Dict[str, Any]] = None,
    *,
    requested_feature: str = "",
    mutation_journal: Optional[FileMutationJournal] = None,
) -> Dict[str, Any]:
    surface = surface or resolve_cargo_bench_surface(real_workload_dir, command)
    cargo_toml = Path(str(surface.get("package_manifest") or real_workload_dir / "Cargo.toml"))
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-allocator-cargo-overlay",
        "cargo_toml": str(cargo_toml),
        "package": surface.get("package"),
        "manifest_package": surface.get("manifest_package"),
        "surface": surface,
        "changed": False,
        "prepared": False,
    }
    if surface.get("error"):
        record["error"] = str(surface.get("error"))
        return record
    if not cargo_toml.exists():
        record["error"] = f"missing Cargo.toml: {cargo_toml}"
        return record
    repo_root = repo_root_from_script()
    unialloc_path = os.path.relpath(repo_root / "unialloc", cargo_toml.parent)
    text = cargo_toml.read_text(encoding="utf-8")
    next_text = text
    if ALLOCATOR_CARGO_OVERLAY_MARKER not in next_text:
        next_text = next_text.rstrip() + f"\n\n{ALLOCATOR_CARGO_OVERLAY_MARKER}\n"
    dependency_blocks = {
        "bench_ourself": {
            "unialloc": f'[dependencies.unialloc]\npath = "{unialloc_path}"\noptional = true\n',
        },
        "bench_jemalloc": {
            "jemallocator": '[dependencies.jemallocator]\nversion = "0.3.2"\noptional = true\n',
        },
        "bench_mimalloc": {
            "mimalloc": '[dependencies.mimalloc]\nversion = "0.1.25"\ndefault-features = false\noptional = true\n',
        },
        "bench_tcmalloc": {
            "tcmalloc": '[dependencies.tcmalloc]\nversion = "0.3.0"\noptional = true\n',
        },
        "bench_snmalloc": {
            "snmalloc-rs": '[dependencies.snmalloc-rs]\nversion = "=0.2.27"\noptional = true\n',
        },
        "bench_ptmalloc": {},
        "bench_scudo": {},
    }
    added_dependencies: List[str] = []
    for name, block in dependency_blocks.get(requested_feature, {}).items():
        if not cargo_toml_has_dependency(next_text, name):
            next_text = next_text.rstrip() + "\n\n" + block
            added_dependencies.append(name)
    # The paper-era external crates are often built with pinned historical
    # toolchains.  Adding UniAlloc as a path dependency can otherwise let Cargo
    # select modern transitive crates (notably lock_api) that no longer compile
    # on the selected nightly.  These direct exact-version pins keep the wrapper
    # honest: the record remains non-claim-grade, but failures after this point
    # are workload/compiler facts instead of dependency-resolution drift.  When
    # an explicit non-legacy toolchain is selected, remove only the exact blocks
    # generated by this adapter so the diagnostic run can resolve modern crates.
    effective_toolchain = effective_rust_toolchain_for_workload(real_workload_dir, command)
    use_old_nightly_pins = "nightly-2021" in effective_toolchain or "nightly-2022" in effective_toolchain
    added_old_nightly_pins: List[str] = []
    removed_old_nightly_pins: List[str] = []
    for name, block in old_nightly_pin_blocks_for_feature(requested_feature).items():
        if use_old_nightly_pins:
            if not added_old_nightly_pins and ALLOCATOR_OLD_NIGHTLY_PIN_MARKER not in next_text:
                next_text = next_text.rstrip() + "\n\n" + ALLOCATOR_OLD_NIGHTLY_PIN_MARKER + "\n"
            next_text, changed = ensure_dependency_table_block(next_text, name, block)
            if changed:
                added_old_nightly_pins.append(name)
            elif not cargo_toml_has_dependency(next_text, name):
                if not added_old_nightly_pins and ALLOCATOR_OLD_NIGHTLY_PIN_MARKER not in next_text:
                    next_text = next_text.rstrip() + "\n\n" + ALLOCATOR_OLD_NIGHTLY_PIN_MARKER + "\n"
                next_text = next_text.rstrip() + "\n\n" + block
                added_old_nightly_pins.append(name)
        else:
            next_text, removed = remove_exact_dependency_table_block(next_text, name, block)
            if removed:
                removed_old_nightly_pins.append(name)
    if removed_old_nightly_pins and ALLOCATOR_OLD_NIGHTLY_PIN_MARKER in next_text:
        next_text = next_text.replace(ALLOCATOR_OLD_NIGHTLY_PIN_MARKER + "\n", "")
    feature_deps = {
        "bench_ourself": ["unialloc"],
        "bench_jemalloc": ["jemallocator"],
        "bench_ptmalloc": [],
        "bench_mimalloc": ["mimalloc"],
        "bench_tcmalloc": ["tcmalloc"],
        "bench_snmalloc": ["snmalloc-rs"],
        "bench_scudo": [],
    }
    added_features: List[str] = []
    routed_features = [requested_feature] if requested_feature else list(feature_deps)
    for feature in routed_features:
        deps = feature_deps.get(feature, [])
        next_text, changed = ensure_feature_line(next_text, feature, deps)
        if changed:
            added_features.append(feature)
    if next_text != text:
        if mutation_journal is not None:
            mutation_journal.snapshot(cargo_toml)
        cargo_toml.write_text(next_text, encoding="utf-8")
        record["changed"] = True
    record.update(
        {
            "prepared": True,
            "unialloc_path": unialloc_path,
            "requested_feature": requested_feature or None,
            "effective_rust_toolchain_for_overlay": effective_toolchain or None,
            "added_dependencies": added_dependencies,
            "added_features": added_features,
            "added_old_nightly_pins": added_old_nightly_pins,
            "removed_old_nightly_pins": removed_old_nightly_pins,
        }
    )
    return record


def selected_bench_name(command: List[str]) -> str:
    for index, token in enumerate(command):
        if token == "--bench" and index + 1 < len(command):
            return str(command[index + 1]).strip()
        prefix = "--bench="
        if token.startswith(prefix):
            return token[len(prefix) :].strip()
    return ""


def crate_root_overlay_insertion_index(text: str) -> int:
    """Return a safe insertion point after leading crate-level inner attrs."""
    offset = 0
    saw_inner_attr = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#!["):
            offset += len(line)
            saw_inner_attr = True
            continue
        if saw_inner_attr and not stripped:
            offset += len(line)
            continue
        break
    return offset


def allocator_bench_overlay_text() -> str:
    return f"""
{ALLOCATOR_BENCH_OVERLAY_MARKER}
#[cfg(feature = "bench_ourself")]
use unialloc::UniAlloc as UniAllocExternalCargoBenchAllocator;
#[cfg(feature = "bench_ourself")]
#[global_allocator]
static UNIALLOC_EXTERNAL_CARGO_BENCH_ALLOCATOR: UniAllocExternalCargoBenchAllocator = UniAllocExternalCargoBenchAllocator;

#[cfg(feature = "bench_jemalloc")]
use jemallocator::Jemalloc as JemallocExternalCargoBenchAllocator;
#[cfg(feature = "bench_jemalloc")]
#[global_allocator]
static JEMALLOC_EXTERNAL_CARGO_BENCH_ALLOCATOR: JemallocExternalCargoBenchAllocator = JemallocExternalCargoBenchAllocator;

#[cfg(feature = "bench_mimalloc")]
#[global_allocator]
static MIMALLOC_EXTERNAL_CARGO_BENCH_ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[cfg(feature = "bench_tcmalloc")]
#[global_allocator]
static TCMALLOC_EXTERNAL_CARGO_BENCH_ALLOCATOR: tcmalloc::TCMalloc = tcmalloc::TCMalloc;

#[cfg(feature = "bench_snmalloc")]
#[global_allocator]
static SNMALLOC_EXTERNAL_CARGO_BENCH_ALLOCATOR: snmalloc_rs::SnMalloc = snmalloc_rs::SnMalloc;

#[cfg(any(feature = "bench_ptmalloc", feature = "bench_scudo"))]
#[global_allocator]
static SYSTEM_EXTERNAL_CARGO_BENCH_ALLOCATOR: std::alloc::System = std::alloc::System;

#[cfg(all(
    feature = "bench_scudo",
    any(
        feature = "bench_ourself",
        feature = "bench_ptmalloc",
        feature = "bench_jemalloc",
        feature = "bench_mimalloc",
        feature = "bench_tcmalloc",
        feature = "bench_snmalloc"
    )
))]
compile_error!("bench_scudo must be the only enabled benchmark allocator feature");

#[cfg(all(feature = "bench_scudo", not(target_os = "linux")))]
compile_error!("bench_scudo requires Linux and a verifiable Scudo runtime");

#[cfg(all(feature = "bench_scudo", target_os = "linux"))]
mod unialloc_external_scudo_runtime_identity {{
    use std::mem::MaybeUninit;
    use std::os::raw::{{c_char, c_int, c_void}};
    use std::ptr;

    #[repr(C)]
    struct DlInfo {{
        dli_fname: *const c_char,
        dli_fbase: *mut c_void,
        dli_sname: *const c_char,
        dli_saddr: *mut c_void,
    }}

    #[link(name = "dl")]
    extern "C" {{
        fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
        fn dladdr(address: *const c_void, info: *mut DlInfo) -> c_int;
    }}

    extern "C" {{
        fn write(fd: c_int, buffer: *const c_void, count: usize) -> isize;
        fn _exit(status: c_int) -> !;
        fn getenv(name: *const c_char) -> *mut c_char;
        fn realpath(path: *const c_char, resolved: *mut c_char) -> *mut c_char;
    }}

    const EXIT_SCUDO_UNVERIFIED: c_int = 86;
    const SUCCESS: &[u8] = b"unialloc: verified Scudo runtime identity\\n";
    const FAILURE: &[u8] = b"unialloc: bench_scudo requires a verified Scudo runtime\\n";
    const SCUDO_IDENTITY_SYMBOL: &[u8] = b"__scudo_print_stats\\0";
    const SCUDO_RUNTIME_LIBRARY_ENV: &[u8] = b"UNIALLOC_SCUDO_RUNTIME_LIBRARY\\0";
    const PATH_CAPACITY: usize = 4096;
    const ALLOCATOR_ABI_SYMBOLS: [&[u8]; 5] = [
        b"malloc\\0",
        b"calloc\\0",
        b"realloc\\0",
        b"free\\0",
        b"posix_memalign\\0",
    ];

    fn emit(message: &[u8]) {{
        unsafe {{
            write(2, message.as_ptr() as *const c_void, message.len());
        }}
    }}

    fn fail_closed() -> ! {{
        emit(FAILURE);
        unsafe {{ _exit(EXIT_SCUDO_UNVERIFIED) }}
    }}

    unsafe fn symbol_provider(symbol: &[u8]) -> Option<DlInfo> {{
        let address = dlsym(ptr::null_mut(), symbol.as_ptr() as *const c_char);
        if address.is_null() {{
            return None;
        }}
        let mut info = MaybeUninit::<DlInfo>::uninit();
        if dladdr(address as *const c_void, info.as_mut_ptr()) == 0 {{
            return None;
        }}
        Some(info.assume_init())
    }}

    fn same_c_path(left: &[c_char], right: &[c_char]) -> bool {{
        for index in 0..std::cmp::min(left.len(), right.len()) {{
            if left[index] != right[index] {{
                return false;
            }}
            if left[index] == 0 {{
                return true;
            }}
        }}
        false
    }}

    unsafe fn provider_matches_configured_runtime(provider: &DlInfo) -> bool {{
        let configured = getenv(SCUDO_RUNTIME_LIBRARY_ENV.as_ptr() as *const c_char);
        if configured.is_null() || *configured == 0 {{
            return true;
        }}
        if provider.dli_fname.is_null() {{
            return false;
        }}
        let mut configured_realpath = [0 as c_char; PATH_CAPACITY];
        let mut provider_realpath = [0 as c_char; PATH_CAPACITY];
        if realpath(configured, configured_realpath.as_mut_ptr()).is_null()
            || realpath(provider.dli_fname, provider_realpath.as_mut_ptr()).is_null()
        {{
            return false;
        }}
        same_c_path(&configured_realpath, &provider_realpath)
    }}

    extern "C" fn verify_scudo_runtime_identity() {{
        let scudo_provider = unsafe {{ symbol_provider(SCUDO_IDENTITY_SYMBOL) }}
            .unwrap_or_else(|| fail_closed());
        if !unsafe {{ provider_matches_configured_runtime(&scudo_provider) }} {{
            fail_closed();
        }}
        for symbol in ALLOCATOR_ABI_SYMBOLS.iter() {{
            let provider = unsafe {{ symbol_provider(symbol) }}.unwrap_or_else(|| fail_closed());
            if provider.dli_fbase != scudo_provider.dli_fbase
                || !unsafe {{ provider_matches_configured_runtime(&provider) }}
            {{
                fail_closed();
            }}
        }}
        emit(SUCCESS);
    }}

    #[used]
    #[link_section = ".init_array"]
    static VERIFY_SCUDO_RUNTIME_IDENTITY: extern "C" fn() = verify_scudo_runtime_identity;
}}
{ALLOCATOR_BENCH_OVERLAY_END_MARKER}
""".lstrip()


def repair_known_benchmark_source_api_drift(
    text: str,
    *,
    surface: Optional[Dict[str, Any]] = None,
) -> tuple[str, List[Dict[str, Any]]]:
    """Apply exact source-era benchmark API repairs for external workloads.

    These are deliberately narrow compatibility edits.  They let diagnostic runs
    reach allocator/timing behavior when an old benchmark source is compiled
    against the old in-tree crate API shape that now expects iterable column
    selectors.  The surrounding adapter record remains non-claim-grade.
    """

    manifest_package = str((surface or {}).get("manifest_package") or "")
    bench_source = str((surface or {}).get("bench_source") or "")
    if manifest_package != "polars":
        return text, []
    replacements: List[tuple[str, str, str]] = []
    helper_insertions: List[tuple[str, str, str]] = []
    if bench_source.endswith("/polars/benches/bench.rs"):
        replacements.extend(
            [
                (
                    'df1.inner_join(&df2, "id", "id")',
                    'df1.inner_join(&df2, ["id"], ["id"])',
                    "polars DataFrame::inner_join column selectors now require IntoIterator",
                ),
                (
                    'df1.groupby("group")',
                    'df1.groupby(["group"])',
                    "polars DataFrame::groupby column selectors now require IntoIterator",
                ),
                (
                    '.select("item")',
                    '.select(["item"])',
                    "polars GroupBy::select column selectors now require IntoIterator",
                ),
            ]
        )
    elif bench_source.endswith("/polars/benches/sort.rs"):
        helper = """
fn unialloc_sort_init_rand(size: usize, null_probability: f64, modulo: i32) -> Int32Chunked {
    (0..size)
        .map(|idx| {
            if null_probability > 0.0 && idx % 10 == 0 {
                None
            } else {
                Some((idx as i32).rem_euclid(modulo))
            }
        })
        .collect()
}
""".strip()
        helper_insertions.append(
            (
                "use polars::prelude::*;\n",
                "use polars::prelude::*;\n\n" + helper + "\n",
                "polars sort benchmark no longer exposes Int32Chunked::init_rand; use a deterministic nullable ChunkedArray fixture",
            )
        )
        replacements.extend(
            [
                (
                    "Int32Chunked::init_rand(size, 0.0, 10)",
                    "unialloc_sort_init_rand(size, 0.0, 10)",
                    "polars sort benchmark fixture helper replaces removed Int32Chunked::init_rand",
                ),
                (
                    "Int32Chunked::init_rand(size, 0.1, 10)",
                    "unialloc_sort_init_rand(size, 0.1, 10)",
                    "polars sort benchmark fixture helper replaces removed Int32Chunked::init_rand",
                ),
            ]
        )
    else:
        return text, []
    repairs: List[Dict[str, Any]] = []
    next_text = text
    for anchor, insertion, reason in helper_insertions:
        helper_name = "unialloc_sort_init_rand"
        if helper_name in next_text:
            continue
        if anchor not in next_text:
            continue
        next_text = next_text.replace(anchor, insertion, 1)
        repairs.append({"old": anchor.rstrip(), "new": helper_name, "reason": reason})
    for old, new, reason in replacements:
        if old not in next_text:
            continue
        next_text = next_text.replace(old, new)
        repairs.append({"old": old, "new": new, "reason": reason})
    return next_text, repairs


def allocator_overlay_at_crate_root(text: str, overlay: str) -> bool:
    """Require the byte-exact current overlay at the crate-root insertion point."""

    overlay_index = text.find(overlay)
    if overlay_index < 0 or text.find(overlay, overlay_index + 1) >= 0:
        return False
    prefix = text[:overlay_index]
    return crate_root_overlay_insertion_index(prefix) == len(prefix)


def remove_known_allocator_bench_overlay(text: str, overlay: str) -> tuple[str, Dict[str, Any]]:
    """Remove one current/legacy adapter overlay so it can be upgraded exactly."""

    marker_count = text.count(ALLOCATOR_BENCH_OVERLAY_MARKER)
    record: Dict[str, Any] = {
        "removed": False,
        "marker_count": marker_count,
        "previous_overlay_kind": None,
    }
    if marker_count != 1:
        record["error"] = f"expected exactly one allocator overlay marker, found {marker_count}"
        return text, record
    marker_index = text.find(ALLOCATOR_BENCH_OVERLAY_MARKER)
    if overlay in text:
        start = text.find(overlay)
        end = start + len(overlay)
        kind = "current-overlay-relocation"
    else:
        end_marker_index = text.find(ALLOCATOR_BENCH_OVERLAY_END_MARKER, marker_index)
        if end_marker_index >= 0:
            start = marker_index
            end = end_marker_index + len(ALLOCATOR_BENCH_OVERLAY_END_MARKER)
            kind = "versioned-legacy-overlay"
        else:
            legacy_end = (
                "static SYSTEM_EXTERNAL_CARGO_BENCH_ALLOCATOR: std::alloc::System = "
                "std::alloc::System;"
            )
            legacy_end_index = text.find(legacy_end, marker_index)
            if legacy_end_index >= 0:
                start = marker_index
                end = legacy_end_index + len(legacy_end)
                kind = "pre-versioned-system-overlay"
            else:
                first_inner_attr = text.find("#![", marker_index)
                if first_inner_attr >= 0:
                    start = marker_index
                    end = first_inner_attr
                    kind = "pre-versioned-overlay-before-inner-attribute"
                else:
                    record["error"] = "unrecognized allocator overlay marker; refusing an ambiguous replacement"
                    return text, record
    while end < len(text) and text[end] in "\r\n":
        end += 1
    previous = text[start:end]
    record.update(
        {
            "removed": True,
            "previous_overlay_kind": kind,
            "previous_overlay_sha256": hashlib.sha256(previous.encode("utf-8")).hexdigest(),
        }
    )
    return text[:start] + text[end:], record


def ensure_allocator_bench_overlay(
    real_workload_dir: Path,
    command: List[str],
    surface: Optional[Dict[str, Any]] = None,
    *,
    mutation_journal: Optional[FileMutationJournal] = None,
) -> Dict[str, Any]:
    surface = surface or resolve_cargo_bench_surface(real_workload_dir, command)
    bench_name = str(surface.get("bench_name") or selected_bench_name(command))
    bench_rs = Path(str(surface.get("bench_source") or "")) if surface.get("bench_source") else Path()
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-allocator-bench-overlay",
        "bench_name": bench_name or None,
        "bench_rs": str(bench_rs) if bench_name else None,
        "package": surface.get("package"),
        "manifest_package": surface.get("manifest_package"),
        "package_manifest": surface.get("package_manifest"),
        "surface": surface,
        "changed": False,
        "prepared": False,
    }
    if surface.get("error"):
        record["error"] = str(surface.get("error"))
        return record
    if not bench_name:
        record["error"] = "cargo bench command is missing --bench, so the benchmark crate root cannot be patched"
        return record
    if not bench_rs.exists():
        record["error"] = f"missing benchmark source: {bench_rs}"
        return record
    original_text = bench_rs.read_text(encoding="utf-8")
    text, api_repairs = repair_known_benchmark_source_api_drift(original_text, surface=surface)
    if api_repairs:
        record["api_compat_repairs"] = api_repairs
    overlay = allocator_bench_overlay_text()
    overlay_sha256 = hashlib.sha256(overlay.encode("utf-8")).hexdigest()
    record["expected_overlay_sha256"] = overlay_sha256
    if allocator_overlay_at_crate_root(text, overlay):
        if text != original_text:
            if mutation_journal is not None:
                mutation_journal.snapshot(bench_rs)
            bench_rs.write_text(text, encoding="utf-8")
            record["changed"] = True
        record.update({"prepared": True, "exact_overlay_verified": True})
        return record

    upgrade: Optional[Dict[str, Any]] = None
    if ALLOCATOR_BENCH_OVERLAY_MARKER in text:
        text, upgrade = remove_known_allocator_bench_overlay(text, overlay)
        record["overlay_upgrade"] = upgrade
        if not upgrade.get("removed"):
            record["error"] = str(upgrade.get("error") or "could not replace stale allocator overlay")
            return record
    insert_at = crate_root_overlay_insertion_index(text)
    next_text = text[:insert_at] + overlay + "\n" + text[insert_at:]
    if not allocator_overlay_at_crate_root(next_text, overlay):
        record["error"] = "current allocator overlay could not be installed at the crate root"
        return record
    if mutation_journal is not None:
        mutation_journal.snapshot(bench_rs)
    bench_rs.write_text(next_text, encoding="utf-8")
    record.update(
        {
            "changed": next_text != original_text,
            "prepared": True,
            "exact_overlay_verified": True,
            "upgraded_existing_overlay": bool(upgrade),
        }
    )
    return record


def inject_cargo_bench_feature(command: List[str], feature: str) -> tuple[List[str], Dict[str, Any]]:
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-feature-route",
        "requested_feature": feature or None,
        "changed": False,
        "routed": False,
    }
    if not feature:
        record["reason"] = "no allocator feature selected"
        return list(command), record
    args = list(command)
    cargo_index = next((index for index, token in enumerate(args) if Path(token).name == "cargo"), -1)
    if cargo_index < 0:
        record["reason"] = "command does not contain cargo"
        return args, record
    bench_index = next((index for index in range(cargo_index + 1, len(args)) if args[index] == "bench"), -1)
    if bench_index < 0:
        record["reason"] = "cargo command is not a cargo bench invocation"
        return args, record
    try:
        separator = args.index("--", bench_index + 1)
    except ValueError:
        separator = len(args)
    search_area = args[bench_index + 1 : separator]
    if "--features" in search_area:
        feature_index = bench_index + 1 + search_area.index("--features")
        if feature_index + 1 < len(args):
            existing = [part for part in re.split(r"[,\s]+", args[feature_index + 1]) if part]
            if feature not in existing:
                existing.append(feature)
                args[feature_index + 1] = ",".join(existing)
                record["changed"] = True
        else:
            args.insert(feature_index + 1, feature)
            record["changed"] = True
    else:
        args[bench_index + 1 : bench_index + 1] = ["--features", feature]
        record["changed"] = True
    record.update({"routed": True, "command": args})
    return args, record


def inject_cargo_bench_reproducibility_flags(
    command: List[str],
    env: Dict[str, str],
    *,
    allow_lockfile_update: bool = False,
) -> tuple[List[str], Dict[str, Any]]:
    """Default external cargo-bench children to locked/offline resolution.

    The paper workload checkouts are exact pinned sources with lockfiles.  On
    historical cargo toolchains, a plain `cargo bench` can spend minutes
    updating and unpacking the legacy git crates.io index before it even starts
    compiling the selected benchmark.  That behavior is not benchmark evidence
    and can write large temporary pack files.  Default to `--locked --offline`
    so real validation either uses the pinned/cached dependency set or fails
    quickly with retained raw stderr.  Allocator feature routing intentionally
    overlays path dependencies/features into historical manifests, so those
    adapter runs use `--offline` without `--locked` and record why the lockfile
    may be refreshed without network access.  Set
    `UNIALLOC_PAPER_CARGO_ALLOW_NETWORK=1` only for an explicit dependency
    preparation step.
    """

    requested_flags = ["--offline"] if allow_lockfile_update else ["--locked", "--offline"]
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-reproducibility-flags",
        "changed": False,
        "routed": False,
        "network_allowed": env_truthy(env.get("UNIALLOC_PAPER_CARGO_ALLOW_NETWORK")),
        "allow_lockfile_update": allow_lockfile_update,
        "requested_flags": requested_flags,
    }
    args = list(command)
    cargo_index = next((index for index, token in enumerate(args) if Path(token).name == "cargo"), -1)
    if cargo_index < 0:
        record["reason"] = "command does not contain cargo"
        return args, record
    bench_index = next((index for index in range(cargo_index + 1, len(args)) if args[index] == "bench"), -1)
    if bench_index < 0:
        record["reason"] = "cargo command is not a cargo bench invocation"
        return args, record
    if record["network_allowed"]:
        record.update({"routed": True, "reason": "network explicitly allowed by UNIALLOC_PAPER_CARGO_ALLOW_NETWORK", "command": args})
        return args, record
    try:
        separator = args.index("--", bench_index + 1)
    except ValueError:
        separator = len(args)
    search_area = args[bench_index + 1 : separator]
    if "--frozen" in search_area:
        record.update({"routed": True, "reason": "command already uses --frozen", "command": args})
        return args, record
    missing_flags = [flag for flag in requested_flags if flag not in search_area]
    if missing_flags:
        args[bench_index + 1 : bench_index + 1] = missing_flags
        record["changed"] = True
    record.update(
        {
            "routed": True,
            "added_flags": missing_flags,
            "command": args,
            "reason": (
                "defaulted allocator-routed cargo bench to offline dependency resolution while permitting lockfile refresh"
                if allow_lockfile_update
                else "defaulted cargo bench to locked/offline dependency resolution"
            ),
        }
    )
    return args, record


def benchmark_required_cargo_features(surface: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return benchmark-native features required to compile a selected bench.

    Allocator feature routing intentionally adds only the allocator feature by
    default.  A few historical benchmark targets also relied on a crate-native
    feature selected by the paper's original cargo invocation; keep those
    compatibility additions source-specific and report them in the wrapper
    provenance so they cannot be mistaken for claim-grade methodology evidence.
    """

    manifest_package = str(surface.get("manifest_package") or "")
    bench_source = str(surface.get("bench_source") or "")
    if manifest_package == "polars" and bench_source.endswith("/polars/benches/groupby.rs"):
        return [
            {
                "feature": "bench",
                "reason": "Polars groupby Criterion benchmark uses the lazy API exported by the crate-native bench feature",
            }
        ]
    return []


def read_local_rust_toolchain(real_workload_dir: Path) -> str:
    plain = real_workload_dir / "rust-toolchain"
    if plain.exists():
        return plain.read_text(encoding="utf-8", errors="replace").strip()
    toml = real_workload_dir / "rust-toolchain.toml"
    if toml.exists():
        return toml.read_text(encoding="utf-8", errors="replace").strip()
    return ""


def inject_cargo_z_flags_for_legacy_nightly(command: List[str], real_workload_dir: Path) -> tuple[List[str], Dict[str, Any]]:
    required_flags = ["namespaced-features", "weak-dep-features", "unstable-options"]
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-legacy-cargo-z-flags",
        "changed": False,
        "routed": False,
        "flags": required_flags,
    }
    args = list(command)
    cargo_index = next((index for index, token in enumerate(args) if Path(token).name == "cargo"), -1)
    if cargo_index < 0:
        record["reason"] = "command does not contain cargo"
        return args, record
    bench_index = next((index for index in range(cargo_index + 1, len(args)) if args[index] == "bench"), -1)
    if bench_index < 0:
        record["reason"] = "cargo command is not a cargo bench invocation"
        return args, record
    local_toolchain = read_local_rust_toolchain(real_workload_dir)
    explicit_toolchain = args[cargo_index + 1] if cargo_index + 1 < len(args) and args[cargo_index + 1].startswith("+") else ""
    inherited_toolchain = os.environ.get("RUSTUP_TOOLCHAIN", "")
    effective_toolchain = (explicit_toolchain[1:] if explicit_toolchain else "") or inherited_toolchain or local_toolchain
    record["local_rust_toolchain"] = local_toolchain or None
    record["explicit_toolchain"] = explicit_toolchain or None
    record["inherited_rust_toolchain"] = inherited_toolchain or None
    record["effective_toolchain"] = effective_toolchain or None
    if "nightly-2021" not in effective_toolchain:
        record["reason"] = "legacy nightly namespaced-features workaround not required"
        return args, record
    present_flags = {
        token[2:]
        for token in args
        if token.startswith("-Z") and len(token) > 2
    }
    present_flags.update(
        args[index + 1]
        for index, token in enumerate(args)
        if token == "-Z" and index + 1 < len(args)
    )
    missing_flags = [flag for flag in required_flags if flag not in present_flags]
    if not missing_flags:
        record.update({"routed": True, "reason": "flags already present", "command": args})
        return args, record
    insertion: List[str] = []
    for flag in missing_flags:
        insertion.extend(["-Z", flag])
    args[bench_index:bench_index] = insertion
    record.update({"changed": True, "routed": True, "added_flags": missing_flags, "command": args})
    return args, record


def prepare_allocator_feature_routing(
    args: argparse.Namespace,
    command: List[str],
    *,
    mutation_journal: Optional[FileMutationJournal] = None,
) -> tuple[List[str], Dict[str, Any]]:
    allocator = normalize_allocator_name(args.allocator)
    feature = allocator_feature(allocator)
    real_workload_dir = Path(args.real_workload_dir).expanduser().resolve() if args.real_workload_dir else Path.cwd()
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json-allocator-routing",
        "real_workload_dir": str(real_workload_dir),
        "allocator": allocator or None,
        "allocator_feature": feature or None,
        "requested": True,
        "prepared": False,
        "changed": False,
        "claim_grade": False,
        "not_claim_grade_reason": (
            "compile-time benchmark allocator hook only; full claim-grade external evidence still "
            "requires all allocators, full paper workload parity, repetitions, and raw provenance"
        ),
    }
    record["allocator_semantics"] = allocator_semantics_record(args)
    if not feature:
        record["error"] = f"unsupported allocator selector for feature routing: {args.allocator}"
        return command, record
    surface = resolve_cargo_bench_surface(real_workload_dir, command)
    effective_toolchain = effective_rust_toolchain_for_workload(real_workload_dir, command)
    dependency_drift = repair_known_workspace_dependency_drift(
        real_workload_dir,
        effective_toolchain=effective_toolchain,
        mutation_journal=mutation_journal,
    )
    cargo = ensure_allocator_cargo_overlay(
        real_workload_dir,
        command,
        surface,
        requested_feature=feature,
        mutation_journal=mutation_journal,
    )
    bench = ensure_allocator_bench_overlay(real_workload_dir, command, surface, mutation_journal=mutation_journal)
    routed_command, route = inject_cargo_bench_feature(command, feature)
    benchmark_feature_routes: List[Dict[str, Any]] = []
    for required in benchmark_required_cargo_features(surface):
        required_feature = required.get("feature", "")
        routed_command, required_route = inject_cargo_bench_feature(routed_command, required_feature)
        required_route["benchmark_required_feature"] = required_feature
        required_route["reason"] = required.get("reason")
        benchmark_feature_routes.append(required_route)
    routed_command, legacy_z_flag = inject_cargo_z_flags_for_legacy_nightly(routed_command, real_workload_dir)
    record.update(
        {
            "prepared": bool(cargo.get("prepared")) and bool(bench.get("prepared")) and bool(route.get("routed")),
            "changed": (
                bool(cargo.get("changed"))
                or bool(bench.get("changed"))
                or bool(route.get("changed"))
                or any(bool(item.get("changed")) for item in benchmark_feature_routes)
                or bool(legacy_z_flag.get("changed"))
                or bool(dependency_drift.get("changed"))
            ),
            "cargo_bench_surface": surface,
            "preparations": [dependency_drift, cargo, bench, route, *benchmark_feature_routes, legacy_z_flag],
            "routed_command": routed_command,
        }
    )
    errors = [
        str(item.get("error"))
        for item in (dependency_drift, cargo, bench, route, *benchmark_feature_routes, legacy_z_flag)
        if item.get("error")
    ]
    if errors:
        record["error"] = "; ".join(errors)
    return routed_command, record


def success_record(args: argparse.Namespace, command: List[str], rows: List[Dict[str, Any]], child_returncode: int) -> Dict[str, Any]:
    ns_values = [float(row["ns_per_iter"]) for row in rows]
    ns_per_iter = geomean(ns_values)
    assert ns_per_iter is not None
    sources = sorted({str(row.get("measurement_source") or "unknown") for row in rows})
    measurement_source = sources[0] if len(sources) == 1 else "mixed_benchmark_timing"
    allocator_semantics = allocator_semantics_record(args)
    record = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json",
        "success": True,
        "benchmark_owned_json": True,
        "measurement_source": measurement_source,
        "measurement_sources": sources,
        "seconds": ns_per_iter / 1_000_000_000.0,
        "ns_per_iter": ns_per_iter,
        "bench_row_count": len(rows),
        "bench_rows": rows,
        "parsed_bench_rows": rows,
        "benchmarks": sorted(
            {
                str(row.get("name") or "").strip()
                for row in rows
                if str(row.get("name") or "").strip()
            }
        ),
        "child_returncode": child_returncode,
        "command": command,
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "run_index": args.run_index,
        "host_system": platform.system(),
        "host_machine": platform.machine(),
        "platform": platform.platform(),
        "bench_name_filter": args.bench_name_filter or None,
        "criterion_dir": str(Path(args.criterion_dir)),
        "claim_grade": False,
        "claim_grade_blockers": unique_strings(
            [
                "cargo-bench JSON wrapper is an adapter bridge only; outer dataset/allocator/provenance gates must pass before claim-grade use",
                *allocator_semantics.get("claim_grade_blockers", []),
            ]
        ),
        "allocator_semantics": allocator_semantics,
        "generated_at": now_iso(),
    }
    if getattr(args, "allocator_routing_record", None):
        record["allocator_routing"] = args.allocator_routing_record
        record["allocator_feature"] = args.allocator_routing_record.get("allocator_feature")
    if getattr(args, "dependency_env_record", None):
        record["dependency_env"] = args.dependency_env_record
    scudo_execution = getattr(args, "_external_scudo_execution_record", None)
    if isinstance(scudo_execution, dict):
        record["scudo_execution_probe"] = scudo_execution
    scudo_env = getattr(args, "_external_scudo_env_record", None)
    if isinstance(scudo_env, dict):
        record["scudo_subprocess_env"] = scudo_env
    scudo_guard = getattr(args, "_external_scudo_guard_probe", None)
    if isinstance(scudo_guard, dict):
        record["scudo_runtime_guard_probe"] = scudo_guard
    return record


def failure_record(
    args: argparse.Namespace,
    command: List[str],
    message: str,
    *,
    child_returncode: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    allocator_semantics = allocator_semantics_record(args)
    record = {
        "schema_version": 1,
        "source": "paper-external-cargo-bench-json",
        "success": False,
        "benchmark_owned_json": True,
        "measurement_source": "libtest_or_criterion_bench_timing",
        "error": message,
        "child_returncode": child_returncode,
        "command": command,
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "run_index": args.run_index,
        "host_system": platform.system(),
        "host_machine": platform.machine(),
        "platform": platform.platform(),
        "bench_name_filter": args.bench_name_filter or None,
        "criterion_dir": str(Path(args.criterion_dir)),
        "claim_grade": False,
        "claim_grade_blockers": unique_strings(
            [
                "cargo-bench JSON wrapper did not produce a successful benchmark-owned timing record",
                *allocator_semantics.get("claim_grade_blockers", []),
            ]
        ),
        "allocator_semantics": allocator_semantics,
        "generated_at": now_iso(),
    }
    if extra:
        record.update(extra)
    if getattr(args, "allocator_routing_record", None):
        record["allocator_routing"] = args.allocator_routing_record
        record["allocator_feature"] = args.allocator_routing_record.get("allocator_feature")
    if getattr(args, "dependency_env_record", None):
        record["dependency_env"] = args.dependency_env_record
    scudo_execution = getattr(args, "_external_scudo_execution_record", None)
    if isinstance(scudo_execution, dict):
        record["scudo_execution_probe"] = scudo_execution
    scudo_env = getattr(args, "_external_scudo_env_record", None)
    if isinstance(scudo_env, dict):
        record["scudo_subprocess_env"] = scudo_env
    scudo_guard = getattr(args, "_external_scudo_guard_probe", None)
    if isinstance(scudo_guard, dict):
        record["scudo_runtime_guard_probe"] = scudo_guard
    return record


class BoundedPipeCapture:
    """Drain a child pipe while retaining only a bounded tail."""

    def __init__(self, pipe: Any, max_bytes: int) -> None:
        self.pipe = pipe
        self.max_bytes = max(1, int(max_bytes))
        self.total_bytes = 0
        self._tail = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float = 5.0) -> None:
        self.thread.join(timeout=timeout)

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.pipe.read(64 * 1024)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                self.total_bytes += len(chunk)
                self._tail.extend(chunk)
                if len(self._tail) > self.max_bytes:
                    del self._tail[: len(self._tail) - self.max_bytes]
        finally:
            try:
                self.pipe.close()
            except Exception:
                pass

    @property
    def retained_bytes(self) -> int:
        return len(self._tail)

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self.retained_bytes

    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")

    def bytes(self) -> bytes:
        return bytes(self._tail)


def run_child_command(
    command: List[str],
    env: Dict[str, str],
    timeout: int,
    max_output_bytes: int,
    *,
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    start_new_session = os.name == "posix"
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(cwd) if cwd is not None and cwd.exists() else None,
        start_new_session=start_new_session,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_capture = BoundedPipeCapture(proc.stdout, max_output_bytes)
    stderr_capture = BoundedPipeCapture(proc.stderr, max_output_bytes)
    stdout_capture.start()
    stderr_capture.start()
    timed_out = False
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if start_new_session:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            proc.terminate()
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if start_new_session:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            returncode = proc.wait(timeout=5)
    finally:
        stdout_capture.join()
        stderr_capture.join()
    return {
        "returncode": 124 if timed_out else int(returncode or 0),
        "stdout": stdout_capture.text(),
        "stderr": stderr_capture.text(),
        "_stdout_retained_bytes": stdout_capture.bytes(),
        "_stderr_retained_bytes": stderr_capture.bytes(),
        "timed_out": timed_out,
        "stdout_bytes": stdout_capture.total_bytes,
        "stderr_bytes": stderr_capture.total_bytes,
        "stdout_retained_bytes": stdout_capture.retained_bytes,
        "stderr_retained_bytes": stderr_capture.retained_bytes,
        "stdout_truncated": stdout_capture.truncated,
        "stderr_truncated": stderr_capture.truncated,
        "max_output_bytes": max_output_bytes,
    }


def parse_cargo_bench_targets_arg(value: str, command: List[str]) -> List[str]:
    targets = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if not targets:
        selected = selected_bench_name(command)
        return [selected] if selected else []
    return list(dict.fromkeys(targets))


def replace_cargo_bench_target(command: List[str], target: str) -> List[str]:
    next_command = list(command)
    for index, token in enumerate(next_command):
        if token == "--bench" and index + 1 < len(next_command):
            next_command[index + 1] = target
            return next_command
    next_command.extend(["--bench", target])
    return next_command


def criterion_estimate_mtimes(criterion_dir: Path) -> Dict[str, float]:
    if not criterion_dir.exists():
        return {}
    mtimes: Dict[str, float] = {}
    for path in criterion_dir.glob("**/new/estimates.json"):
        try:
            mtimes[str(path.resolve())] = path.stat().st_mtime
        except OSError:
            continue
    return mtimes


def only_new_or_updated_criterion_rows(
    rows: List[Dict[str, Any]],
    before_mtimes: Dict[str, float],
) -> List[Dict[str, Any]]:
    if not before_mtimes:
        return rows
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        path_text = str(row.get("criterion_estimates_path") or "")
        if not path_text:
            filtered.append(row)
            continue
        path = Path(path_text)
        try:
            after_mtime = path.stat().st_mtime
            key = str(path.resolve())
        except OSError:
            continue
        before_mtime = before_mtimes.get(key)
        if before_mtime is None or after_mtime > before_mtime:
            filtered.append(row)
    return filtered


def run(args: argparse.Namespace) -> int:
    command = [str(part) for part in args.command]
    args.allocator_routing_record = None
    args.dependency_env_record = None
    if not command:
        emit(failure_record(args, command, "missing child cargo bench command"))
        return 2
    requested_targets = parse_cargo_bench_targets_arg(args.cargo_bench_targets, command)
    real_workload_dir = Path(args.real_workload_dir).expanduser().resolve() if args.real_workload_dir else Path.cwd().resolve()
    requested_scudo_sources = scudo_request_sources(args, command)
    args._external_scudo_request_sources = requested_scudo_sources
    if command_requests_scudo_feature(command) and allocator_feature(args.allocator) != "bench_scudo":
        emit(
            failure_record(
                args,
                command,
                "bench_scudo cargo feature conflicts with the non-Scudo allocator selector",
                extra={
                    "scudo_request_sources": requested_scudo_sources,
                    "measurement_eligible": False,
                    "child_started": False,
                },
            )
        )
        return 2
    if requested_scudo_sources:
        scudo_execution = prepare_scudo_execution(args, command, real_workload_dir)
        if not scudo_execution.get("ok"):
            emit(
                failure_record(
                    args,
                    command,
                    "no executable Scudo runtime route is available; child timing was not started",
                    extra={
                        "scudo_execution_probe": scudo_execution,
                        "measurement_eligible": False,
                        "child_started": False,
                    },
                )
            )
            return 2
        if not args.allocator_feature_routing:
            emit(
                failure_record(
                    args,
                    command,
                    "bench_scudo requires allocator feature routing so the fail-closed runtime identity guard is installed; child timing was not started",
                    extra={
                        "scudo_execution_probe": scudo_execution,
                        "allocator_feature_routing_required": True,
                        "measurement_eligible": False,
                        "child_started": False,
                    },
                )
            )
            return 2
    if args.dry_run:
        allocator_semantics = allocator_semantics_record(args)
        record = {
            "schema_version": 1,
            "source": "paper-external-cargo-bench-json",
            "success": True,
            "dry_run": True,
            "benchmark_owned_json": True,
            "measurement_source": "libtest_or_criterion_bench_timing",
            "command": command,
            "dataset": args.dataset,
            "benchmark": args.benchmark,
            "allocator": args.allocator,
            "variant_feature": args.variant_feature,
            "run_index": args.run_index,
            "bench_name_filter": args.bench_name_filter or None,
            "cargo_bench_targets": requested_targets,
            "multi_target": bool(args.cargo_bench_targets),
            "criterion_dir": str(Path(args.criterion_dir)),
            "allocator_feature_routing_requested": bool(args.allocator_feature_routing),
            "allocator_feature": allocator_feature(args.allocator) if args.allocator_feature_routing else None,
            "allocator_semantics": allocator_semantics,
            "claim_grade": False,
            "claim_grade_blockers": unique_strings(
                [
                    "cargo-bench JSON wrapper dry-run is not timing evidence",
                    *allocator_semantics.get("claim_grade_blockers", []),
                ]
            ),
            "generated_at": now_iso(),
        }
        scudo_execution = getattr(args, "_external_scudo_execution_record", None)
        if isinstance(scudo_execution, dict):
            record["scudo_execution_probe"] = scudo_execution
        emit(record)
        return 0
    mutation_journal = FileMutationJournal(real_workload_dir) if args.allocator_feature_routing else None

    def finish(record: Dict[str, Any], code: int) -> int:
        if mutation_journal is not None:
            cleanup = mutation_journal.restore()
            record["checkout_cleanup"] = cleanup
            if getattr(args, "allocator_routing_record", None):
                args.allocator_routing_record["checkout_cleanup"] = cleanup
                record["allocator_routing"] = args.allocator_routing_record
            if not cleanup.get("success"):
                blockers = list(record.get("claim_grade_blockers") or [])
                blockers.append("external workload checkout cleanup failed after allocator feature routing")
                record["claim_grade_blockers"] = unique_strings(blockers)
                record["claim_grade"] = False
                record["checkout_cleanup_failed"] = True
                if code == 0:
                    record["success"] = False
                    record["error"] = "external workload checkout cleanup failed after child cargo bench command"
                    code = 3
        emit(record)
        return code

    env = os.environ.copy()
    env.setdefault("UNIALLOC_PAPER_DATASET", str(args.dataset or ""))
    env.setdefault("UNIALLOC_PAPER_BENCHMARK", str(args.benchmark or ""))
    env.setdefault("UNIALLOC_PAPER_ALLOCATOR", str(args.allocator or ""))
    env.setdefault("UNIALLOC_PAPER_VARIANT_FEATURE", str(args.variant_feature or ""))
    env.setdefault("UNIALLOC_PAPER_RUN_INDEX", str(args.run_index))
    max_output_bytes = positive_int_value(getattr(args, "max_output_bytes", None), 16 * 1024 * 1024)
    evidence_dir = resolve_evidence_dir(str(getattr(args, "evidence_dir", "") or ""), args)
    if requested_scudo_sources and evidence_dir is None:
        evidence_dir = Path(tempfile.mkdtemp(prefix="paper-external-cargo-scudo-evidence-"))
    all_evidence: List[Dict[str, Any]] = []
    args.dependency_env_record = configure_dependency_env(args, env)
    if requested_scudo_sources:
        scudo_env_record = apply_scudo_subprocess_env(args, env)
        args._external_scudo_env_record = scudo_env_record
        if not scudo_env_record.get("ok"):
            return finish(
                failure_record(
                    args,
                    command,
                    "Scudo runtime environment could not be applied; child timing was not started",
                    extra={
                        "scudo_subprocess_env": scudo_env_record,
                        "measurement_eligible": False,
                        "child_started": False,
                    },
                ),
                2,
            )
    polars_csv_fixture = maybe_prepare_polars_csv_fixture(args, env, requested_targets=requested_targets)
    if polars_csv_fixture is not None:
        args.dependency_env_record["polars_csv_fixture"] = polars_csv_fixture
        args.dependency_env_record["csv_src"] = env.get("CSV_SRC")
    all_rows: List[Dict[str, Any]] = []
    target_records: List[Dict[str, Any]] = []
    for target_index, target in enumerate(requested_targets or [selected_bench_name(command) or ""]):
        target_command = replace_cargo_bench_target(command, target) if target else list(command)
        routing_record: Optional[Dict[str, Any]] = None
        reproducibility_flags: Optional[Dict[str, Any]] = None
        if args.allocator_feature_routing:
            target_command, routing_record = prepare_allocator_feature_routing(
                args,
                target_command,
                mutation_journal=mutation_journal,
            )
            if target_index == 0:
                args.allocator_routing_record = routing_record
            if not routing_record.get("prepared"):
                return finish(
                    failure_record(
                        args,
                        target_command,
                        "allocator feature routing could not prepare the cargo bench surface",
                        extra={
                            "allocator_routing": routing_record,
                            "cargo_bench_target": target or None,
                            "cargo_bench_targets": requested_targets,
                            "target_records": target_records,
                        },
                    ),
                    2,
                )
        scudo_guard_probe: Optional[Dict[str, Any]] = None
        if requested_scudo_sources:
            scudo_guard_probe = scudo_runtime_guard_source_probe(real_workload_dir, target_command)
            args._external_scudo_guard_probe = scudo_guard_probe
            if not scudo_guard_probe.get("ok"):
                return finish(
                    failure_record(
                        args,
                        target_command,
                        "selected Scudo benchmark has no verifiable runtime identity guard; child timing was not started",
                        extra={
                            "cargo_bench_target": target or None,
                            "cargo_bench_targets": requested_targets,
                            "scudo_runtime_guard_probe": scudo_guard_probe,
                            "measurement_eligible": False,
                            "child_started": False,
                            "target_records": target_records,
                        },
                    ),
                    2,
                )
        target_command, reproducibility_flags = inject_cargo_bench_reproducibility_flags(
            target_command,
            env,
            allow_lockfile_update=bool(args.allocator_feature_routing),
        )
        started_at = time.time()
        criterion_dir = Path(args.criterion_dir)
        before_criterion = criterion_estimate_mtimes(criterion_dir)
        try:
            child_result = run_child_command(
                target_command,
                env,
                args.timeout,
                max_output_bytes,
                cwd=real_workload_dir,
            )
        except KeyboardInterrupt as exc:
            interrupted_result = getattr(exc, "_unialloc_bounded_child_result", None)
            if isinstance(interrupted_result, dict):
                forward_interrupted_child_output(interrupted_result)
                interruption_record = cargo_child_interruption_record(
                    args,
                    target_command,
                    interrupted_result,
                )
                emit(interruption_record)
                setattr(exc, "_unialloc_interruption_record", interruption_record)
            raise
        stdout_retained = child_result.pop("_stdout_retained_bytes", b"")
        stderr_retained = child_result.pop("_stderr_retained_bytes", b"")
        child_returncode = int(child_result.get("returncode") or 0)
        child_stdout = str(child_result.get("stdout") or "")
        child_stderr = str(child_result.get("stderr") or "")
        child_timed_out = bool(child_result.get("timed_out"))
        target_label = slugify_label(target or selected_bench_name(target_command) or f"target-{target_index + 1}", f"target-{target_index + 1}")
        target_evidence = [
            entry
            for entry in (
                write_evidence_file(
                    evidence_dir,
                    f"{target_index + 1:03d}-{target_label}.stdout.retained.txt",
                    stdout_retained if isinstance(stdout_retained, bytes) else bytes(str(stdout_retained), "utf-8"),
                    "cargo_bench_stdout_retained",
                ),
                write_evidence_file(
                    evidence_dir,
                    f"{target_index + 1:03d}-{target_label}.stderr.retained.txt",
                    stderr_retained if isinstance(stderr_retained, bytes) else bytes(str(stderr_retained), "utf-8"),
                    "cargo_bench_stderr_retained",
                ),
            )
            if entry
        ]
        all_evidence.extend(target_evidence)
        if child_stdout:
            sys.stdout.write(child_stdout)
            if not child_stdout.endswith("\n"):
                sys.stdout.write("\n")
        if child_stderr:
            sys.stderr.write(child_stderr)
            if not child_stderr.endswith("\n"):
                sys.stderr.write("\n")
        verified_scudo_execution: Optional[Dict[str, Any]] = None
        if requested_scudo_sources and not child_timed_out and child_returncode == 0:
            verified_scudo_execution = verify_scudo_runtime_identity(args, child_stderr)
            if not verified_scudo_execution.get("runtime_verified"):
                failure = failure_record(
                    args,
                    target_command,
                    "Scudo child exited successfully without the required runtime identity marker; timing was rejected",
                    child_returncode=child_returncode,
                    extra={
                        "cargo_bench_target": target or None,
                        "cargo_bench_targets": requested_targets,
                        "scudo_execution_probe": verified_scudo_execution,
                        "required_runtime_identity_marker": SCUDO_RUNTIME_IDENTITY_MARKER,
                        "measurement_eligible": False,
                        "max_output_bytes": max_output_bytes,
                        "stdout_bytes": child_result.get("stdout_bytes"),
                        "stderr_bytes": child_result.get("stderr_bytes"),
                        "stdout_retained_bytes": child_result.get("stdout_retained_bytes"),
                        "stderr_retained_bytes": child_result.get("stderr_retained_bytes"),
                        "stdout_truncated": child_result.get("stdout_truncated"),
                        "stderr_truncated": child_result.get("stderr_truncated"),
                        "partial_bench_row_count": len(all_rows),
                        "partial_bench_rows": all_rows,
                        "target_records": target_records,
                    },
                )
                append_evidence(failure, all_evidence)
                if evidence_dir is not None:
                    failure["raw_evidence_dir"] = str(evidence_dir)
                return finish(failure, 1)
            if routing_record is not None:
                routing_record["allocator_semantics"] = allocator_semantics_record(args)
                routing_record["scudo_execution_probe"] = verified_scudo_execution
        criterion_rows = parse_criterion_estimates(
            criterion_dir,
            started_at=started_at,
            name_filter=args.bench_name_filter,
            include_stale=args.include_stale_criterion,
        )
        if args.cargo_bench_targets and not args.include_stale_criterion:
            criterion_rows = only_new_or_updated_criterion_rows(criterion_rows, before_criterion)
        rows = [
            *parse_bench_rows(child_stdout, name_filter=args.bench_name_filter),
            *criterion_rows,
        ]
        for row in rows:
            row["cargo_bench_target"] = target or selected_bench_name(target_command) or None
        target_record = {
            "cargo_bench_target": target or selected_bench_name(target_command) or None,
            "command": target_command,
            "child_returncode": child_returncode,
            "timed_out": child_timed_out,
            "max_output_bytes": max_output_bytes,
            "stdout_bytes": child_result.get("stdout_bytes"),
            "stderr_bytes": child_result.get("stderr_bytes"),
            "stdout_retained_bytes": child_result.get("stdout_retained_bytes"),
            "stderr_retained_bytes": child_result.get("stderr_retained_bytes"),
            "stdout_truncated": child_result.get("stdout_truncated"),
            "stderr_truncated": child_result.get("stderr_truncated"),
            "bench_row_count": len(rows),
            "measurement_sources": sorted({str(row.get("measurement_source") or "unknown") for row in rows}),
        }
        append_evidence(target_record, target_evidence)
        if routing_record is not None:
            target_record["allocator_routing"] = routing_record
        if reproducibility_flags is not None:
            target_record["reproducibility_flags"] = reproducibility_flags
        if verified_scudo_execution is not None:
            target_record["scudo_execution_probe"] = verified_scudo_execution
            target_record["allocator_semantics"] = allocator_semantics_record(args)
        if scudo_guard_probe is not None:
            target_record["scudo_runtime_guard_probe"] = scudo_guard_probe
        target_records.append(target_record)
        if child_timed_out:
            failure = failure_record(
                args,
                target_command,
                "child cargo bench command timed out",
                child_returncode=child_returncode,
                extra={
                    "timeout_seconds": args.timeout,
                    "timed_out": True,
                    "max_output_bytes": max_output_bytes,
                    "stdout_bytes": child_result.get("stdout_bytes"),
                    "stderr_bytes": child_result.get("stderr_bytes"),
                    "stdout_retained_bytes": child_result.get("stdout_retained_bytes"),
                    "stderr_retained_bytes": child_result.get("stderr_retained_bytes"),
                    "stdout_truncated": child_result.get("stdout_truncated"),
                    "stderr_truncated": child_result.get("stderr_truncated"),
                    "cargo_bench_target": target or None,
                    "cargo_bench_targets": requested_targets,
                    "partial_bench_row_count": len(all_rows),
                    "partial_bench_rows": all_rows,
                    "target_records": target_records,
                },
            )
            append_evidence(failure, all_evidence)
            if evidence_dir is not None:
                failure["raw_evidence_dir"] = str(evidence_dir)
            return finish(failure, 124)
        if child_returncode != 0:
            failure = failure_record(
                args,
                target_command,
                "child cargo bench command failed",
                child_returncode=child_returncode,
                extra={
                    "cargo_bench_target": target or None,
                    "cargo_bench_targets": requested_targets,
                    "max_output_bytes": max_output_bytes,
                    "stdout_bytes": child_result.get("stdout_bytes"),
                    "stderr_bytes": child_result.get("stderr_bytes"),
                    "stdout_retained_bytes": child_result.get("stdout_retained_bytes"),
                    "stderr_retained_bytes": child_result.get("stderr_retained_bytes"),
                    "stdout_truncated": child_result.get("stdout_truncated"),
                    "stderr_truncated": child_result.get("stderr_truncated"),
                    "partial_bench_row_count": len(all_rows),
                    "partial_bench_rows": all_rows,
                    "target_records": target_records,
                },
            )
            append_evidence(failure, all_evidence)
            if evidence_dir is not None:
                failure["raw_evidence_dir"] = str(evidence_dir)
            return finish(failure, child_returncode or 1)
        if not rows:
            failure = failure_record(
                args,
                target_command,
                "child output did not contain libtest bench ns/iter rows or fresh Criterion estimates.json timing",
                child_returncode=child_returncode,
                extra={
                    "cargo_bench_target": target or None,
                    "cargo_bench_targets": requested_targets,
                    "max_output_bytes": max_output_bytes,
                    "stdout_bytes": child_result.get("stdout_bytes"),
                    "stderr_bytes": child_result.get("stderr_bytes"),
                    "stdout_retained_bytes": child_result.get("stdout_retained_bytes"),
                    "stderr_retained_bytes": child_result.get("stderr_retained_bytes"),
                    "stdout_truncated": child_result.get("stdout_truncated"),
                    "stderr_truncated": child_result.get("stderr_truncated"),
                    "partial_bench_row_count": len(all_rows),
                    "partial_bench_rows": all_rows,
                    "target_records": target_records,
                },
            )
            append_evidence(failure, all_evidence)
            if evidence_dir is not None:
                failure["raw_evidence_dir"] = str(evidence_dir)
            return finish(failure, 2)
        all_rows.extend(rows)
    if not all_rows:
        failure = failure_record(
            args,
            command,
            "child output did not contain libtest bench ns/iter rows or fresh Criterion estimates.json timing",
            child_returncode=0,
            extra={"cargo_bench_targets": requested_targets, "target_records": target_records},
        )
        append_evidence(failure, all_evidence)
        if evidence_dir is not None:
            failure["raw_evidence_dir"] = str(evidence_dir)
        return finish(failure, 2)
    record = success_record(args, command, all_rows, 0)
    record["cargo_bench_targets"] = requested_targets
    record["multi_target"] = bool(args.cargo_bench_targets)
    record["target_records"] = target_records
    record["max_output_bytes"] = max_output_bytes
    record["stdout_bytes"] = sum(int(item.get("stdout_bytes") or 0) for item in target_records)
    record["stderr_bytes"] = sum(int(item.get("stderr_bytes") or 0) for item in target_records)
    record["stdout_retained_bytes"] = sum(int(item.get("stdout_retained_bytes") or 0) for item in target_records)
    record["stderr_retained_bytes"] = sum(int(item.get("stderr_retained_bytes") or 0) for item in target_records)
    record["stdout_truncated"] = any(bool(item.get("stdout_truncated")) for item in target_records)
    record["stderr_truncated"] = any(bool(item.get("stderr_truncated")) for item in target_records)
    append_evidence(record, all_evidence)
    if evidence_dir is not None:
        record["raw_evidence_dir"] = str(evidence_dir)
    apply_success_claim_grade_contract(record, args, requested_targets=requested_targets)
    if requested_scudo_sources:
        _paper_workload_driver.attach_scudo_timing_binding(
            record,
            evidence_dir=evidence_dir,
        )
    return finish(record, 0)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature", default="")
    parser.add_argument("--run-index", default="1")
    parser.add_argument("--bench-name-filter", default="")
    parser.add_argument(
        "--cargo-bench-targets",
        default="",
        help="Comma-separated cargo bench targets to run sequentially; each target replaces the child command's --bench value",
    )
    parser.add_argument(
        "--claim-grade-contract",
        default="off",
        choices=("off", "roxipng-paper-fragment", "rpolars-paper-run", "rjs-compiler-paper-run", "rpython-paper-run"),
        help=(
            "Enable a strict claim-grade contract for successful wrapper output; "
            "default off leaves the wrapper diagnostic-only"
        ),
    )
    parser.add_argument(
        "--expected-cargo-bench-targets",
        default="",
        help="Comma-separated full expected cargo bench target surface for claim-grade contract checks",
    )
    parser.add_argument(
        "--expected-libtest-bench-functions",
        default="",
        help="Comma-separated expected libtest functions for the selected full target or leaf fragment",
    )
    parser.add_argument(
        "--paper-source-contract-json",
        default="",
        help="JSON object proving exact paper source/ref/checkout provenance for claim-grade contract checks",
    )
    parser.add_argument(
        "--paper-source-json",
        default="",
        help="Optional paper_source JSON object to embed in the emitted sample record",
    )
    parser.add_argument(
        "--rpolars-input-contract-json",
        default="",
        help="JSON object emitted by the R-Polars input provenance audit for claim-grade R-Polars runs",
    )
    parser.add_argument("--criterion-dir", default="target/criterion")
    parser.add_argument(
        "--include-stale-criterion",
        action="store_true",
        help="Allow pre-existing Criterion estimates files; default only accepts files touched by this child run",
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-output-bytes", type=int, default=16 * 1024 * 1024, help="Retain at most this many child stdout/stderr bytes per stream")
    parser.add_argument(
        "--evidence-dir",
        default=os.environ.get("UNIALLOC_PAPER_EVIDENCE_DIR", ""),
        help=(
            "Base directory for retained child stdout/stderr evidence. A unique per-run "
            "subdirectory is created and evidence entries include sha256 digests."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate wrapper command shape without executing child cargo bench")
    parser.add_argument(
        "--allocator-feature-routing",
        action="store_true",
        help="Patch the selected cargo bench target with a feature-gated global allocator and run cargo bench with the selected allocator feature",
    )
    parser.add_argument(
        "--real-workload-dir",
        help="Checkout root to patch for allocator feature routing; default is current working directory",
    )
    parser.add_argument(
        "--scudo-mode",
        choices=_paper_workload_driver.SCUDO_MODES,
        default=os.environ.get("UNIALLOC_SCUDO_MODE", _paper_workload_driver.SCUDO_DEFAULT_MODE),
        help=(
            "Canonical Scudo execution route: rust-sanitizer uses rustc support, "
            "ld-preload uses a standalone runtime, and auto selects an available route"
        ),
    )
    parser.add_argument(
        "--scudo-runtime-library",
        default=os.environ.get("UNIALLOC_SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_STANDALONE_LIBRARY"),
        help="Path or directory containing libclang_rt.scudo_standalone-*.so",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Child cargo bench command after --")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
