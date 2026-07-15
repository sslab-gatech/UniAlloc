#!/usr/bin/env python3
"""Build a reproducible RustSec heap-security screening inventory.

The existing 40-case corpus is a purposive evaluation subset.  This command
keeps that frozen corpus intact while screening the complete pinned RustSec
advisory database for the larger research queue.  Screening labels identify
manual-review candidates; they are not mitigation or detection results.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tomllib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "evaluation" / "config" / "rustsec_heap_security_corpus.json"
DEFAULT_OUTPUT = ROOT / "evaluation" / "config" / "rustsec_heap_candidate_inventory.json"

FRONT_MATTER_RE = re.compile(r"\A```toml\s*\n(.*?)\n```", re.S)
TITLE_RE = re.compile(r"^#\s+(.+)$", re.M)

# These signals intentionally inspect only the advisory title and explicit
# keywords.  Body-text matches are useful for discovery but too vulnerable to
# incidental mentions of other bug classes for a stable screening artifact.
SIGNAL_PATTERNS = {
    "use_after_free": re.compile(
        r"\buse[- ]after[- ]free\b|\bdangling (?:pointer|reference|ref|value)\b",
        re.I,
    ),
    "double_free": re.compile(
        r"\bdouble[- ]free\b|\bdouble[- ]drop\b|"
        r"\bdrop(?:s|ped|ping)? .{0,24}\btwice\b",
        re.I,
    ),
    "invalid_deallocation": re.compile(
        r"\b(?:invalid|bad)[- ](?:free|deallocation)\b|"
        r"\bincorrect deallocation\b|"
        r"\bwrong deallocation\b|\bmisaligned allocation\b",
        re.I,
    ),
    "out_of_bounds": re.compile(
        r"\bout[- ]of[- ]bounds\b|\boob\b|"
        r"\b(?:heap |stack )?buffer (?:over|under)flow\b|"
        r"\bbuffer overrun\b|"
        r"\b(?:read|write) past (?:the )?(?:end|allocated area)\b",
        re.I,
    ),
    "uninitialized_memory": re.compile(
        r"\buninitiali[sz]ed\b|\buninit(?:ialized)?\b",
        re.I,
    ),
}

MECHANISM_QUEUES = {
    "use_after_free": "cross_type_reuse_grooming_review",
    "double_free": "tracked_reclaim_review",
    "invalid_deallocation": "recovery_and_tag_validation_review",
    "out_of_bounds": "guard_page_eligibility_review",
    "uninitialized_memory": "force_initialize_eligibility_review",
}


class InventoryError(RuntimeError):
    """Raised when the pinned inventory cannot be reproduced safely."""


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InventoryError(f"expected a list of strings, observed {value!r}")
    return list(value)


def _json_scalar(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head(path: pathlib.Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def parse_advisory(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    front_matter = FRONT_MATTER_RE.match(text)
    if front_matter is None:
        raise InventoryError(f"missing TOML front matter: {path}")
    try:
        document = tomllib.loads(front_matter.group(1))
    except tomllib.TOMLDecodeError as error:
        raise InventoryError(f"invalid TOML front matter in {path}: {error}") from error
    advisory = document.get("advisory")
    if not isinstance(advisory, dict):
        raise InventoryError(f"missing advisory table: {path}")
    advisory_id = advisory.get("id")
    package = advisory.get("package")
    if not isinstance(advisory_id, str) or not isinstance(package, str):
        raise InventoryError(f"missing advisory id/package: {path}")
    heading = TITLE_RE.search(text[front_matter.end() :])
    if heading is None:
        raise InventoryError(f"missing advisory title: {path}")
    versions = document.get("versions", {})
    if not isinstance(versions, dict):
        raise InventoryError(f"invalid versions table: {path}")
    return {
        "advisory": advisory,
        "advisory_id": advisory_id,
        "categories": _string_list(advisory.get("categories")),
        "keywords": _string_list(advisory.get("keywords")),
        "package": package,
        "title": heading.group(1).strip(),
        "versions": versions,
        "document_text": text,
    }


def parse_rudra_poc(path: pathlib.Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```rudra-poc\n(.*?)\n```", text, re.S)
    if match is None:
        return None
    try:
        document = tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError as error:
        raise InventoryError(f"invalid Rudra-PoC metadata in {path}: {error}") from error
    target = document.get("target")
    report = document.get("report", {})
    bugs = document.get("bugs", [])
    if not isinstance(target, dict) or not isinstance(report, dict):
        raise InventoryError(f"invalid Rudra-PoC target/report metadata: {path}")
    if not isinstance(bugs, list) or not all(isinstance(bug, dict) for bug in bugs):
        raise InventoryError(f"invalid Rudra-PoC bug metadata: {path}")
    return {
        "target": target,
        "report": report,
        "bugs": bugs,
    }


def classification_signals(title: str, keywords: list[str]) -> list[str]:
    searchable = f"{title} {' '.join(keywords)}"
    return [
        label for label, pattern in SIGNAL_PATTERNS.items() if pattern.search(searchable)
    ]


def manifest_screen_match(
    categories: list[str], keywords: list[str], query: dict[str, Any]
) -> bool:
    category_any = set(_string_list(query.get("category_any")))
    keyword_any = {value.lower() for value in _string_list(query.get("keyword_any"))}
    return bool(
        set(categories) & category_any
        or {value.lower() for value in keywords} & keyword_any
    )


def _count(entries: list[dict[str, Any]], predicate) -> int:
    return sum(1 for entry in entries if predicate(entry))


def build_inventory(
    rustsec_db: pathlib.Path,
    corpus_path: pathlib.Path = DEFAULT_CORPUS,
    *,
    rudra_poc: pathlib.Path | None = None,
    verify_commit: bool = True,
) -> dict[str, Any]:
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    source = corpus.get("source_snapshots", {}).get("rustsec_advisory_db")
    if not isinstance(source, dict):
        raise InventoryError("corpus lacks source_snapshots.rustsec_advisory_db")
    expected_commit = source.get("commit")
    if not isinstance(expected_commit, str):
        raise InventoryError("corpus lacks a pinned RustSec commit")
    observed_commit = git_head(rustsec_db)
    if verify_commit and observed_commit != expected_commit:
        raise InventoryError(
            f"RustSec checkout commit mismatch: expected {expected_commit}, "
            f"observed {observed_commit}"
        )

    query = source.get("candidate_screen_query")
    if not isinstance(query, dict):
        raise InventoryError("corpus lacks a candidate_screen_query")
    selected_cases = corpus.get("cases")
    if not isinstance(selected_cases, list):
        raise InventoryError("corpus cases must be a list")
    selected_by_advisory = {
        case["advisory_id"]: case["case_id"]
        for case in selected_cases
        if isinstance(case, dict)
        and isinstance(case.get("advisory_id"), str)
        and isinstance(case.get("case_id"), str)
    }

    advisory_root = rustsec_db / "crates"
    advisory_files = sorted(advisory_root.glob("*/RUSTSEC-*.md"))
    if not advisory_files:
        raise InventoryError(f"no RustSec advisory files found beneath {advisory_root}")

    all_entries: list[dict[str, Any]] = []
    active_ids: set[str] = set()
    active_manifest_screen_ids: set[str] = set()
    active_expanded_screen_ids: set[str] = set()
    active_manifest_or_body_signal_ids: set[str] = set()
    active_high_recall_ids: set[str] = set()
    active_unsound_ids: set[str] = set()
    for path in advisory_files:
        parsed = parse_advisory(path)
        advisory = parsed["advisory"]
        categories = parsed["categories"]
        keywords = parsed["keywords"]
        signals = classification_signals(parsed["title"], keywords)
        original_screen = manifest_screen_match(categories, keywords, query)
        expanded_screen = original_screen or bool(signals)
        body_direct_primitive_screen = any(
            pattern.search(parsed["document_text"])
            for pattern in SIGNAL_PATTERNS.values()
        )
        active = not advisory.get("withdrawn") and advisory.get("informational") not in {
            "unmaintained",
            "notice",
        }
        advisory_id = parsed["advisory_id"]
        if active:
            active_ids.add(advisory_id)
            if original_screen:
                active_manifest_screen_ids.add(advisory_id)
            if expanded_screen:
                active_expanded_screen_ids.add(advisory_id)
            if original_screen or body_direct_primitive_screen:
                active_manifest_or_body_signal_ids.add(advisory_id)
            if advisory.get("informational") == "unsound":
                active_unsound_ids.add(advisory_id)
            if (
                original_screen
                or body_direct_primitive_screen
                or advisory.get("informational") == "unsound"
            ):
                active_high_recall_ids.add(advisory_id)
        if not expanded_screen:
            continue
        relative_path = path.relative_to(rustsec_db).as_posix()
        all_entries.append(
            {
                "advisory_id": advisory_id,
                "package": parsed["package"],
                "title": parsed["title"],
                "date": _json_scalar(advisory.get("date")),
                "informational": advisory.get("informational"),
                "withdrawn": _json_scalar(advisory.get("withdrawn")),
                "categories": categories,
                "keywords": keywords,
                "patched_versions": _string_list(parsed["versions"].get("patched")),
                "unaffected_versions": _string_list(
                    parsed["versions"].get("unaffected")
                ),
                "source_path": relative_path,
                "source_sha256": file_sha256(path),
                "current_corpus_case_id": selected_by_advisory.get(advisory_id),
                "manifest_candidate_screen": original_screen,
                "expanded_title_keyword_screen": expanded_screen,
                "body_direct_primitive_screen": body_direct_primitive_screen,
                "active": active,
                "classification_signals": signals,
                "mechanism_review_queues": [MECHANISM_QUEUES[label] for label in signals],
                "manual_review_required": True,
            }
        )

    all_entries.sort(key=lambda entry: entry["advisory_id"])
    temporal_reclaim = {"use_after_free", "double_free", "invalid_deallocation"}
    signal_counts = {
        signal: _count(
            all_entries,
            lambda entry, signal=signal: signal in entry["classification_signals"],
        )
        for signal in SIGNAL_PATTERNS
    }
    signal_selected_counts = {
        signal: _count(
            all_entries,
            lambda entry, signal=signal: signal in entry["classification_signals"]
            and entry["current_corpus_case_id"] is not None,
        )
        for signal in SIGNAL_PATTERNS
    }
    temporal_reclaim_count = _count(
        all_entries,
        lambda entry: bool(temporal_reclaim & set(entry["classification_signals"])),
    )
    temporal_reclaim_selected_count = _count(
        all_entries,
        lambda entry: bool(temporal_reclaim & set(entry["classification_signals"]))
        and entry["current_corpus_case_id"] is not None,
    )
    current_selected_ids = set(selected_by_advisory)
    expanded_ids = {entry["advisory_id"] for entry in all_entries}
    manifest_screen_ids = {
        entry["advisory_id"]
        for entry in all_entries
        if entry["manifest_candidate_screen"]
    }

    result: dict[str, Any] = {
        "schema_version": 1,
        "source": "unialloc-rustsec-heap-candidate-inventory",
        "claim_grade": False,
        "claim_boundary": (
            "Automated title/category/keyword screening creates a manual-review queue. "
            "It does not establish allocator visibility, reproducibility, Type Isolation "
            "eligibility, mitigation, or detection."
        ),
        "rustsec_source": {
            "url": source.get("url"),
            "commit": expected_commit,
            "commit_time": source.get("commit_time"),
            "observed_commit": observed_commit,
            "advisory_file_count": len(advisory_files),
            "candidate_screen_query": query,
        },
        "screen_definition": {
            "manifest_screen": "exact category/keyword query frozen in the 40-case corpus",
            "expanded_screen": (
                "manifest screen union high-precision advisory-title/keyword signals"
            ),
            "classification_signal_patterns": {
                label: pattern.pattern for label, pattern in SIGNAL_PATTERNS.items()
            },
        },
        "summary": {
            "parsed_advisory_count": len(advisory_files),
            "active_advisory_count": len(active_ids),
            "memory_corruption_category_count": source.get(
                "memory_corruption_category_count"
            ),
            "manifest_candidate_screen_count": len(manifest_screen_ids),
            "active_manifest_candidate_screen_count": len(
                active_manifest_screen_ids
            ),
            "expanded_candidate_screen_count": len(all_entries),
            "active_expanded_candidate_screen_count": len(
                active_expanded_screen_ids
            ),
            "active_manifest_or_body_direct_primitive_count": len(
                active_manifest_or_body_signal_ids
            ),
            "active_unsound_advisory_count": len(active_unsound_ids),
            "active_high_recall_review_count": len(active_high_recall_ids),
            "expanded_candidates_outside_manifest_screen_count": len(
                expanded_ids - manifest_screen_ids
            ),
            "current_corpus_case_count": len(current_selected_ids),
            "current_corpus_in_manifest_screen_count": len(
                current_selected_ids & manifest_screen_ids
            ),
            "current_corpus_in_expanded_screen_count": len(
                current_selected_ids & expanded_ids
            ),
            "current_corpus_outside_expanded_screen_count": len(
                current_selected_ids - expanded_ids
            ),
            "unselected_manifest_candidates": len(
                manifest_screen_ids - current_selected_ids
            ),
            "unselected_expanded_candidates": len(expanded_ids - current_selected_ids),
            "temporal_reclaim_title_keyword_candidates": temporal_reclaim_count,
            "temporal_reclaim_selected_in_current_corpus": (
                temporal_reclaim_selected_count
            ),
            "temporal_reclaim_unselected": (
                temporal_reclaim_count - temporal_reclaim_selected_count
            ),
            "classification_signal_counts": signal_counts,
            "classification_signal_selected_counts": signal_selected_counts,
        },
        "candidates": all_entries,
    }
    if rudra_poc is not None:
        rudra_source = corpus.get("source_snapshots", {}).get("rudra_poc")
        if not isinstance(rudra_source, dict):
            raise InventoryError("corpus lacks source_snapshots.rudra_poc")
        expected_rudra_commit = rudra_source.get("commit")
        observed_rudra_commit = git_head(rudra_poc)
        if verify_commit and observed_rudra_commit != expected_rudra_commit:
            raise InventoryError(
                "Rudra-PoC checkout commit mismatch: "
                f"expected {expected_rudra_commit}, observed {observed_rudra_commit}"
            )
        rudra_files = sorted((rudra_poc / "poc").glob("*.rs"))
        rudra_entries: list[dict[str, Any]] = []
        for path in rudra_files:
            parsed = parse_rudra_poc(path)
            if parsed is None:
                continue
            target = parsed["target"]
            report = parsed["report"]
            rustsec_id = report.get("rustsec_id")
            bug_classes = sorted(
                {
                    str(bug["bug_class"])
                    for bug in parsed["bugs"]
                    if isinstance(bug.get("bug_class"), str)
                }
            )
            rudra_entries.append(
                {
                    "source_path": path.relative_to(rudra_poc).as_posix(),
                    "source_sha256": file_sha256(path),
                    "target_crate": target.get("crate"),
                    "target_version": target.get("version"),
                    "rustsec_id": rustsec_id,
                    "bug_classes": bug_classes,
                    "selected_in_current_corpus": (
                        isinstance(rustsec_id, str) and rustsec_id in current_selected_ids
                    ),
                }
            )
        mapped_ids = {
            entry["rustsec_id"]
            for entry in rudra_entries
            if isinstance(entry["rustsec_id"], str)
        }
        selected_mapped_ids = mapped_ids & current_selected_ids
        panic_safety_ids = {
            entry["rustsec_id"]
            for entry in rudra_entries
            if isinstance(entry["rustsec_id"], str)
            and "PanicSafety" in entry["bug_classes"]
        }
        result["rudra_poc_source"] = {
            "url": rudra_source.get("url"),
            "commit": expected_rudra_commit,
            "commit_time": rudra_source.get("commit_time"),
            "observed_commit": observed_rudra_commit,
            "rust_source_file_count": len(rudra_files),
        }
        result["rudra_poc_summary"] = {
            "parseable_poc_count": len(rudra_entries),
            "rustsec_mapped_poc_count": _count(
                rudra_entries, lambda entry: isinstance(entry["rustsec_id"], str)
            ),
            "unique_rustsec_id_count": len(mapped_ids),
            "selected_unique_rustsec_id_count": len(selected_mapped_ids),
            "unselected_unique_rustsec_id_count": len(mapped_ids - current_selected_ids),
            "panic_safety_unique_rustsec_id_count": len(panic_safety_ids),
            "panic_safety_selected_unique_rustsec_id_count": len(
                panic_safety_ids & current_selected_ids
            ),
            "panic_safety_unselected_unique_rustsec_id_count": len(
                panic_safety_ids - current_selected_ids
            ),
        }
        result["rudra_pocs"] = rudra_entries
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustsec-db", required=True, type=pathlib.Path)
    parser.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--rudra-poc",
        type=pathlib.Path,
        help="optional pinned Rudra-PoC checkout used to inventory public reproducers",
    )
    parser.add_argument(
        "--allow-commit-mismatch",
        action="store_true",
        help="screen an exploratory checkout whose HEAD differs from the pinned corpus",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        inventory = build_inventory(
            args.rustsec_db,
            args.corpus,
            rudra_poc=args.rudra_poc,
            verify_commit=not args.allow_commit_mismatch,
        )
    except (InventoryError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(inventory["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
