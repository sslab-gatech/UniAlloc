#!/usr/bin/env python3
"""Probe for a redis-benchmark executable without mutating the host.

The RRedis paper bridge needs the upstream Redis client tool, but the
evaluation harness should not silently install system packages.  This helper
records exactly which candidate was selected, or why none was usable, so the
RRedis audits can distinguish "tool missing" from "paper methodology missing".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation"
RAW = EVAL / "raw"
RESULTS = EVAL / "results"
DEFAULT_REDIS_RELEASE_VERSION = "7.2.5"
DEFAULT_REDIS_RELEASE_URL = f"https://download.redis.io/releases/redis-{DEFAULT_REDIS_RELEASE_VERSION}.tar.gz"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_strings(values: Iterable[Any]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def executable_status(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {"exists": False, "executable": False}
    exists = path.exists()
    return {
        "exists": exists,
        "is_file": path.is_file() if exists else False,
        "executable": os.access(path, os.X_OK) if exists else False,
    }


def resolve_path(raw: Any, *, root: Path = ROOT) -> Optional[Path]:
    text = str(raw or "").strip()
    if not text:
        return None
    expanded = Path(text).expanduser()
    if expanded.is_absolute() or "/" in text:
        return (expanded if expanded.is_absolute() else root / expanded).resolve()
    found = shutil.which(text)
    return Path(found).resolve() if found else None


def version_probe(path: Path, *, timeout: float = 5.0) -> Dict[str, Any]:
    for args in (["--version"], ["-v"]):
        try:
            proc = subprocess.run(
                [str(path), *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "args": args,
            }
        text = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
        if proc.returncode == 0 and text:
            return {
                "ok": True,
                "args": args,
                "returncode": proc.returncode,
                "text": text.splitlines()[0][:240],
                "stdout_tail": proc.stdout[-1000:],
                "stderr_tail": proc.stderr[-1000:],
            }
    return {
        "ok": False,
        "error": "redis-benchmark version probe produced no usable output",
        "args": ["--version", "-v"],
    }


def add_candidate(
    candidates: List[Dict[str, Any]],
    *,
    source: str,
    raw: Any,
    path: Optional[Path],
    note: str = "",
    discovery_error: str = "",
) -> None:
    record: Dict[str, Any] = {
        "source": source,
        "raw": str(raw or ""),
        "path": str(path) if path else None,
        "note": note,
    }
    record.update(executable_status(path))
    if discovery_error:
        record["discovery_error"] = discovery_error
    candidates.append(record)


def brew_prefix(redis_formula: str = "redis") -> Dict[str, Any]:
    brew = shutil.which("brew")
    if not brew:
        return {"ok": False, "error": "brew is not on PATH"}
    try:
        proc = subprocess.run(
            [brew, "--prefix", redis_formula],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    prefix = proc.stdout.strip()
    if proc.returncode != 0 or not prefix:
        return {
            "ok": False,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-1000:],
            "stderr_tail": proc.stderr[-1000:],
            "error": "brew prefix redis did not resolve an installed prefix",
        }
    return {"ok": True, "prefix": prefix}


def redis_benchmark_candidates(
    explicit: Optional[str] = None,
    *,
    root: Path = ROOT,
    include_environment: bool = True,
    include_path: bool = True,
    include_repo_local: bool = True,
    include_homebrew: bool = True,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    if explicit:
        add_candidate(
            candidates,
            source="explicit-argument",
            raw=explicit,
            path=resolve_path(explicit, root=root),
            note="value passed by --redis-benchmark-bin",
        )

    env_value = os.environ.get("UNIALLOC_REDIS_BENCHMARK_BIN", "").strip() if include_environment else ""
    if include_environment and env_value:
        add_candidate(
            candidates,
            source="environment",
            raw=env_value,
            path=resolve_path(env_value, root=root),
            note="UNIALLOC_REDIS_BENCHMARK_BIN",
        )

    if include_path:
        path_value = shutil.which("redis-benchmark")
        add_candidate(
            candidates,
            source="path",
            raw="redis-benchmark",
            path=Path(path_value).resolve() if path_value else None,
            note="first redis-benchmark found on PATH",
        )

    if include_repo_local:
        repo_candidates = [
            root / "evaluation" / "external" / "_deps" / "redis" / "bin" / "redis-benchmark",
            root / "evaluation" / "external" / "_deps" / "redis" / "src" / "redis-benchmark",
            root / "evaluation" / "external" / "_deps" / "redis" / "redis-benchmark",
            root / "evaluation" / "external" / "_deps" / "redis-src" / "src" / "redis-benchmark",
        ]
        for path in repo_candidates:
            add_candidate(
                candidates,
                source="repo-local",
                raw=str(path.relative_to(root)),
                path=path.resolve(),
                note="ignored repository-local dependency location",
            )

    if include_homebrew:
        for raw in (
            "/opt/homebrew/opt/redis/bin/redis-benchmark",
            "/usr/local/opt/redis/bin/redis-benchmark",
        ):
            add_candidate(
                candidates,
                source="homebrew-opt",
                raw=raw,
                path=Path(raw).resolve(),
                note="stable Homebrew opt path",
            )

        brew = brew_prefix()
        if brew.get("ok"):
            path = Path(str(brew["prefix"])) / "bin" / "redis-benchmark"
            add_candidate(
                candidates,
                source="homebrew-prefix",
                raw=str(path),
                path=path.resolve(),
                note="brew --prefix redis",
            )
        else:
            add_candidate(
                candidates,
                source="homebrew-prefix",
                raw="brew --prefix redis",
                path=None,
                note="brew prefix probe",
                discovery_error=str(brew.get("error") or brew),
            )

    deduped: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (str(candidate.get("source") or ""), str(candidate.get("path") or candidate.get("raw") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def download_file(url: str, dest: Path, *, force: bool = False, timeout: float = 60.0) -> Dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        return {
            "downloaded": False,
            "url": url,
            "path": str(dest),
            "bytes": dest.stat().st_size,
            "sha256": sha256_file(dest),
            "reason": "cached tarball already exists",
        }
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "UniAlloc-evaluation/1.0"})
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    tmp.replace(dest)
    return {
        "downloaded": True,
        "url": url,
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "sha256": sha256_file(dest),
        "duration_seconds": round(time.time() - started, 3),
    }


def safe_extract_tarball(tarball: Path, dest: Path) -> List[str]:
    dest.mkdir(parents=True, exist_ok=True)
    extracted_roots: set[str] = set()
    with tarfile.open(tarball, "r:gz") as tar:
        dest_resolved = dest.resolve()
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if dest_resolved not in (target, *target.parents):
                raise ValueError(f"refusing to extract tar member outside destination: {member.name}")
            first = Path(member.name).parts[0] if Path(member.name).parts else ""
            if first:
                extracted_roots.add(first)
        tar.extractall(dest)
    return sorted(extracted_roots)


def run_build_command(command: List[str], *, cwd: Path, timeout: float = 300.0) -> Dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "command": command,
            "cwd": str(cwd),
            "returncode": proc.returncode,
            "ok": proc.returncode == 0,
            "duration_seconds": round(time.time() - started, 3),
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "cwd": str(cwd),
            "returncode": 124,
            "ok": False,
            "duration_seconds": round(time.time() - started, 3),
            "error": f"timeout after {timeout}s: {exc}",
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }


def bootstrap_repo_local_redis_benchmark(
    *,
    root: Path = ROOT,
    version: str = DEFAULT_REDIS_RELEASE_VERSION,
    url: str = DEFAULT_REDIS_RELEASE_URL,
    force: bool = False,
    keep_source: bool = False,
    build_timeout: float = 300.0,
) -> Dict[str, Any]:
    """Build redis-benchmark into the ignored repo-local dependency prefix.

    This intentionally avoids installing system packages.  It is still
    non-claim evidence: the resulting client proves tool availability for the
    RRedis bridge, while exact paper command provenance and Linux host parity
    remain separate gates.
    """
    deps = root / "evaluation" / "external" / "_deps"
    prefix = deps / "redis"
    target_bin = prefix / "bin" / "redis-benchmark"
    source_root = deps / f"redis-src-{version}"
    tarball = deps / "redis-cache" / f"redis-{version}.tar.gz"
    record: Dict[str, Any] = {
        "source": "repo-local-redis-benchmark-bootstrap",
        "version": version,
        "url": url,
        "prefix": str(prefix),
        "target_bin": str(target_bin),
        "force": force,
        "keep_source": keep_source,
        "claim_grade": False,
        "claim_grade_blockers": [
            "repo-local redis-benchmark availability is tool evidence only; exact paper command provenance, repetitions, Linux host parity, and allocator runtime equivalence remain separate RRedis gates"
        ],
    }
    if target_bin.exists() and os.access(target_bin, os.X_OK) and not force:
        record.update(
            {
                "skipped": True,
                "reason": "repo-local redis-benchmark already exists",
                "target_sha256": sha256_file(target_bin),
                "version_probe": version_probe(target_bin),
            }
        )
        return record

    if source_root.exists():
        shutil.rmtree(source_root)
    source_root.mkdir(parents=True, exist_ok=True)
    try:
        record["download"] = download_file(url, tarball, force=force)
        roots = safe_extract_tarball(tarball, source_root)
        record["extracted_roots"] = roots
        if not roots:
            raise ValueError("redis release tarball did not contain a source root")
        build_dir = source_root / roots[0]
        jobs = str(max(1, min(4, os.cpu_count() or 1)))
        make = shutil.which("make") or "make"
        build = run_build_command(
            [make, "-j", jobs, "redis-benchmark"],
            cwd=build_dir,
            timeout=build_timeout,
        )
        record["build"] = build
        built_bin = build_dir / "src" / "redis-benchmark"
        if not build.get("ok") or not built_bin.exists():
            record["available"] = False
            record["error"] = "redis-benchmark build failed or did not produce src/redis-benchmark"
            return record
        target_bin.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built_bin, target_bin)
        target_bin.chmod(target_bin.stat().st_mode | 0o111)
        record.update(
            {
                "available": True,
                "target_sha256": sha256_file(target_bin),
                "version_probe": version_probe(target_bin),
            }
        )
        return record
    except Exception as exc:
        record.update({"available": False, "error": f"{type(exc).__name__}: {exc}"})
        return record
    finally:
        if not keep_source and source_root.exists():
            shutil.rmtree(source_root)
            record["source_tree_removed_after_build"] = True


def probe_redis_benchmark_tool(
    explicit: Optional[str] = None,
    *,
    root: Path = ROOT,
    include_environment: bool = True,
    include_path: bool = True,
    include_repo_local: bool = True,
    include_homebrew: bool = True,
) -> Dict[str, Any]:
    candidates = redis_benchmark_candidates(
        explicit,
        root=root,
        include_environment=include_environment,
        include_path=include_path,
        include_repo_local=include_repo_local,
        include_homebrew=include_homebrew,
    )
    selected: Optional[Dict[str, Any]] = None
    for candidate in candidates:
        if candidate.get("exists") is True and candidate.get("executable") is True and candidate.get("path"):
            selected = candidate
            break

    version: Dict[str, Any] = {}
    if selected:
        version = version_probe(Path(str(selected["path"])))
        selected["version_probe"] = version

    blockers: List[str] = []
    if not selected:
        blockers.append("redis-benchmark executable was not found in explicit, environment, PATH, repo-local, or Homebrew candidates")
    else:
        blockers.append("redis-benchmark availability alone is not paper-grade RRedis evidence; exact paper command, repetitions, and provenance remain separate gates")

    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "source": "redis-benchmark-tool-probe",
        "tool": "redis-benchmark",
        "available": selected is not None,
        "selected_path": selected.get("path") if selected else None,
        "selected_source": selected.get("source") if selected else None,
        "selected_version": version.get("text") if version.get("ok") else None,
        "recommended_config_value": selected.get("path") if selected else "redis-benchmark",
        "claim_grade": False,
        "claim_grade_blockers": unique_strings(blockers),
        "preparation_plan": {
            "repo_local_prefix": str(root / "evaluation" / "external" / "_deps" / "redis"),
            "system_install_not_attempted": True,
            "notes": [
                "Place a redis-benchmark executable at evaluation/external/_deps/redis/bin/redis-benchmark, set UNIALLOC_REDIS_BENCHMARK_BIN, or install the Homebrew redis formula.",
                "This probe is intentionally read-only; it records availability but does not install packages or build Redis.",
            ],
        },
        "candidates": candidates,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-benchmark-bin", help="Explicit redis-benchmark path or command name")
    parser.add_argument(
        "--bootstrap-repo-local",
        action="store_true",
        help="Download a fixed Redis release and build redis-benchmark into evaluation/external/_deps/redis/bin without installing system packages",
    )
    parser.add_argument(
        "--redis-release-version",
        default=DEFAULT_REDIS_RELEASE_VERSION,
        help=f"Redis release version used with --bootstrap-repo-local; default {DEFAULT_REDIS_RELEASE_VERSION}",
    )
    parser.add_argument(
        "--redis-release-url",
        default=None,
        help="Override Redis release tarball URL used with --bootstrap-repo-local",
    )
    parser.add_argument(
        "--force-bootstrap",
        action="store_true",
        help="Re-download/rebuild the repo-local redis-benchmark even if a candidate already exists",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Keep the extracted Redis source tree after bootstrap; default removes it to save disk",
    )
    parser.add_argument(
        "--build-timeout",
        type=float,
        default=300.0,
        help="Maximum seconds for the repo-local redis-benchmark build",
    )
    parser.add_argument("--run-id", help="Raw artifact directory name; default is timestamped")
    parser.add_argument("--output-dir", help="Output directory; default is evaluation/raw/<run-id>")
    parser.add_argument("--results-out", help="Optional additional JSON artifact path")
    parser.add_argument(
        "--no-update-results",
        action="store_true",
        help="Write only the raw probe artifact; do not refresh evaluation/results/redis_benchmark_tool_probe.json",
    )
    args = parser.parse_args(argv)

    run_id = args.run_id or _dt.datetime.now().strftime("redis-benchmark-tool-probe-%Y%m%d-%H%M%S")
    out_dir = Path(args.output_dir).expanduser() if args.output_dir else RAW / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_artifact = None
    if args.bootstrap_repo_local:
        url = args.redis_release_url or f"https://download.redis.io/releases/redis-{args.redis_release_version}.tar.gz"
        bootstrap_artifact = bootstrap_repo_local_redis_benchmark(
            version=str(args.redis_release_version),
            url=str(url),
            force=bool(args.force_bootstrap),
            keep_source=bool(args.keep_source),
            build_timeout=float(args.build_timeout),
        )
    artifact = probe_redis_benchmark_tool(args.redis_benchmark_bin)
    if bootstrap_artifact is not None:
        artifact["bootstrap"] = bootstrap_artifact
        if bootstrap_artifact.get("available") and not artifact.get("available"):
            artifact = probe_redis_benchmark_tool(str(bootstrap_artifact.get("target_bin")))
            artifact["bootstrap"] = bootstrap_artifact
    artifact["run_id"] = run_id
    artifact_path = out_dir / "redis-benchmark-tool-probe.json"
    write_json(artifact_path, artifact)
    published: List[str] = []
    if not args.no_update_results:
        results_path = RESULTS / "redis_benchmark_tool_probe.json"
        write_json(results_path, artifact)
        published.append(str(results_path))
    if args.results_out:
        results_out = Path(args.results_out).expanduser()
        write_json(results_out, artifact)
        published.append(str(results_out))
    if published:
        artifact["published_results"] = published
        write_json(artifact_path, artifact)
        if not args.no_update_results:
            write_json(RESULTS / "redis_benchmark_tool_probe.json", artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
