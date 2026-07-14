#!/usr/bin/env python3
"""Render auditable allocator evidence from completed real-world campaigns."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import pathlib
import shlex
import statistics
import sys
from collections.abc import Mapping, Sequence
from typing import Any


REQUIRED_VARIANTS = (
    "unialloc",
    "mimalloc",
    "tcmalloc",
    "typed_plain",
    "typeiso_perf",
)
VARIANT_LABELS = {
    "native": "Native",
    "system": "System",
    "jemalloc": "jemalloc",
    "mimalloc": "mimalloc",
    "tcmalloc": "TCMalloc",
    "unialloc": "UniAlloc",
    "typed_plain": "Typed plain",
    "typeiso_perf": "Type Isolation",
    "typeiso_coverage": "Type Isolation coverage",
}
COMPARISONS = (
    ("unialloc", "mimalloc"),
    ("unialloc", "tcmalloc"),
    ("typeiso_perf", "typed_plain"),
)
SOURCE_PROVENANCE_FIELDS = (
    "checkouts",
    "corpus",
    "fd_tree",
    "wrapper",
    "sysroot",
)
class ReportError(RuntimeError):
    """An invalid or incomplete campaign input."""


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_campaign_spec(value: str) -> tuple[str, pathlib.Path]:
    app, separator, raw_path = value.partition("=")
    if not separator or not app.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("campaign must use APP=PATH")
    return app.strip(), pathlib.Path(raw_path).expanduser()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign",
        action="append",
        required=True,
        type=parse_campaign_spec,
        metavar="APP=PATH",
        help="completed single-application matrix result; repeat for each app",
    )
    parser.add_argument(
        "--markdown-out",
        "--markdown",
        dest="markdown_out",
        type=pathlib.Path,
        required=True,
    )
    parser.add_argument(
        "--json-out",
        "--evidence-json",
        dest="json_out",
        type=pathlib.Path,
        required=True,
    )
    parser.add_argument(
        "--title",
        default="Real-world Rust allocator evaluation",
    )
    return parser.parse_args(argv)


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportError(f"{label} must be an object")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReportError(f"{label} must be an array")
    return value


def require_close(actual: Any, expected: Any, label: str) -> None:
    if not math.isclose(
        float(actual),
        float(expected),
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ReportError(f"{label} does not match raw measurements")


def validate_campaign_measurements(document: Mapping[str, Any], app: str) -> None:
    variants = [
        str(value) for value in require_list(document.get("variants"), "variants")
    ]
    if not variants or len(set(variants)) != len(variants):
        raise ReportError(f"campaign {app} has invalid variants")
    warmups = int(document.get("warmups", -1))
    repetitions = int(document.get("repetitions", -1))
    if warmups < 0 or repetitions < 1:
        raise ReportError(f"campaign {app} has invalid sample counts")

    rows_by_variant: dict[str, list[Mapping[str, Any]]] = {
        variant: [] for variant in variants
    }
    output_hashes: set[str] = set()
    for index, raw_row in enumerate(
        require_list(document.get("measurements"), "measurements")
    ):
        row = require_mapping(raw_row, f"measurement[{index}]")
        variant = str(row.get("variant", ""))
        if variant not in rows_by_variant:
            raise ReportError(f"campaign {app} has unexpected measurement variant")
        if (
            row.get("exit_code") != 0
            or row.get("gnu_time_exit_status") != 0
            or row.get("timed_out") is not False
        ):
            raise ReportError(f"campaign {app} has an unsuccessful measurement")
        output_hash = row.get("output_sha256")
        if not isinstance(output_hash, str) or not output_hash:
            raise ReportError(f"campaign {app} has a missing output hash")
        output_hashes.add(output_hash)
        rows_by_variant[variant].append(row)
    if len(output_hashes) != 1:
        raise ReportError(f"campaign {app} outputs are not equivalent")

    summaries = summary_by_variant(document)
    if set(summaries) != set(variants):
        raise ReportError(f"campaign {app} summaries do not match configured variants")
    for variant, rows in rows_by_variant.items():
        warmup_rows = [row for row in rows if row.get("warmup") is True]
        measured_rows = [row for row in rows if row.get("warmup") is False]
        if len(warmup_rows) != warmups or len(measured_rows) != repetitions:
            raise ReportError(f"campaign {app}/{variant} has an invalid sample count")
        if sorted(row.get("measurement_index") for row in measured_rows) != list(
            range(repetitions)
        ):
            raise ReportError(
                f"campaign {app}/{variant} has invalid measurement indexes"
            )
        summary = summaries[variant]
        if int(summary.get("sample_count", -1)) != repetitions:
            raise ReportError(f"campaign {app}/{variant} summary count is stale")

        wall = [float(row["wall_seconds"]) for row in measured_rows]
        rss = [float(row["peak_rss_kib"]) for row in measured_rows]
        total_cpu = [
            float(row["user_cpu_seconds"]) + float(row["system_cpu_seconds"])
            for row in measured_rows
        ]
        work_amounts = {float(row["work_amount"]) for row in measured_rows}
        work_units = {str(row["work_unit"]) for row in measured_rows}
        if len(work_amounts) != 1 or len(work_units) != 1:
            raise ReportError(f"campaign {app}/{variant} changes workload shape")
        work_amount = next(iter(work_amounts))
        throughput = [work_amount / value for value in wall]
        if next(iter(work_units)) == "bytes":
            throughput = [value / (1024 * 1024) for value in throughput]

        expected_fields = {
            "median_wall_seconds": statistics.median(wall),
            "wall_mad_seconds": statistics.median(
                abs(value - statistics.median(wall)) for value in wall
            ),
            "min_wall_seconds": min(wall),
            "max_wall_seconds": max(wall),
            "median_user_cpu_seconds": statistics.median(
                float(row["user_cpu_seconds"]) for row in measured_rows
            ),
            "median_system_cpu_seconds": statistics.median(
                float(row["system_cpu_seconds"]) for row in measured_rows
            ),
            "median_total_cpu_seconds": statistics.median(total_cpu),
            "median_cpu_percent": statistics.median(
                float(row["cpu_percent"]) for row in measured_rows
            ),
            "median_peak_rss_kib": statistics.median(rss),
            "min_peak_rss_kib": min(rss),
            "max_peak_rss_kib": max(rss),
            "rss_mad_kib": statistics.median(
                abs(value - statistics.median(rss)) for value in rss
            ),
            "median_major_page_faults": statistics.median(
                float(row["major_page_faults"]) for row in measured_rows
            ),
            "median_minor_page_faults": statistics.median(
                float(row["minor_page_faults"]) for row in measured_rows
            ),
            "median_involuntary_context_switches": statistics.median(
                float(row["involuntary_context_switches"])
                for row in measured_rows
            ),
            "median_voluntary_context_switches": statistics.median(
                float(row["voluntary_context_switches"]) for row in measured_rows
            ),
            "median_throughput_per_second": statistics.median(throughput),
        }
        for field, expected in expected_fields.items():
            require_close(
                summary.get(field), expected, f"campaign {app}/{variant} {field}"
            )
        if summary.get("output_sha256") != next(iter(output_hashes)):
            raise ReportError(f"campaign {app}/{variant} summary output is stale")

    for subject, reference in COMPARISONS:
        subject_rows = {
            int(row["measurement_index"]): row
            for row in rows_by_variant[subject]
            if row.get("warmup") is False
        }
        reference_rows = {
            int(row["measurement_index"]): row
            for row in rows_by_variant[reference]
            if row.get("warmup") is False
        }
        for field, prefix in (
            ("wall_seconds", "paired_wall_ratio_vs_"),
            ("peak_rss_kib", "paired_rss_ratio_vs_"),
        ):
            expected = statistics.median(
                float(subject_rows[index][field])
                / float(reference_rows[index][field])
                for index in range(repetitions)
            )
            require_close(
                summaries[subject].get(prefix + reference),
                expected,
                f"campaign {app}/{subject} paired {field}",
            )


def load_campaign(app: str, path: pathlib.Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as error:
        raise ReportError(f"unable to read campaign for {app}: {resolved}") from error
    except json.JSONDecodeError as error:
        raise ReportError(f"invalid JSON campaign for {app}: {resolved}: {error}") from error
    document = dict(require_mapping(value, f"campaign {app}"))
    if document.get("success") is not True:
        raise ReportError(f"campaign {app} is not successful")
    apps = require_list(document.get("apps"), f"campaign {app}.apps")
    if apps != [app]:
        raise ReportError(
            f"campaign {app} must contain exactly one matching app; got {apps!r}"
        )
    summaries = require_list(document.get("summaries"), f"campaign {app}.summaries")
    measurements = require_list(
        document.get("measurements"), f"campaign {app}.measurements"
    )
    builds = require_list(document.get("builds"), f"campaign {app}.builds")
    if not summaries or not measurements or not builds:
        raise ReportError(f"campaign {app} has incomplete evidence")
    for collection_name, rows in (
        ("summaries", summaries),
        ("measurements", measurements),
        ("builds", builds),
    ):
        for index, row in enumerate(rows):
            mapping = require_mapping(row, f"campaign {app}.{collection_name}[{index}]")
            if mapping.get("app", app) != app:
                raise ReportError(
                    f"campaign {app}.{collection_name}[{index}] belongs to "
                    f"{mapping.get('app')!r}"
                )
    summary_variants = {
        str(require_mapping(row, "summary").get("variant")) for row in summaries
    }
    missing = sorted(set(REQUIRED_VARIANTS) - summary_variants)
    if missing:
        raise ReportError(
            f"campaign {app} is missing required summary variants: {', '.join(missing)}"
        )
    require_mapping(document.get("host"), f"campaign {app}.host")
    require_mapping(
        document.get("measurement_affinity"),
        f"campaign {app}.measurement_affinity",
    )
    validate_campaign_measurements(document, app)
    build_provenance(document)
    document["_input_path"] = str(resolved)
    document["_input_sha256"] = sha256_file(resolved)
    return document


def summary_by_variant(campaign: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    for raw_row in require_list(campaign.get("summaries"), "summaries"):
        row = require_mapping(raw_row, "summary")
        variant = str(row.get("variant", ""))
        if not variant:
            raise ReportError("summary has no variant")
        if variant in rows:
            raise ReportError(f"duplicate summary variant: {variant}")
        rows[variant] = row
    return rows


def build_provenance(campaign: Mapping[str, Any]) -> list[dict[str, Any]]:
    configured_variants = [
        str(value) for value in require_list(campaign.get("variants"), "variants")
    ]
    records: dict[str, dict[str, Any]] = {}
    for raw_build in require_list(campaign.get("builds"), "builds"):
        build = require_mapping(raw_build, "build")
        variant = str(build.get("variant", ""))
        if not variant:
            raise ReportError("build provenance has no variant")
        if variant in records:
            raise ReportError(f"duplicate build provenance variant: {variant}")
        records[variant] = copy.deepcopy(dict(build))
    missing = sorted(set(configured_variants) - records.keys())
    if missing:
        raise ReportError("missing build provenance: " + ", ".join(missing))
    extra = sorted(records.keys() - set(configured_variants))
    if extra:
        raise ReportError("unexpected build provenance: " + ", ".join(extra))
    if not all(record.get("success") is True for record in records.values()):
        raise ReportError("build provenance contains an unsuccessful build")

    system = records.get("system")
    tcmalloc = records.get("tcmalloc")
    if tcmalloc is not None and system is None:
        raise ReportError("TCMalloc build provenance requires a System build")
    if system is not None and tcmalloc is not None:
        if system.get("binary_sha256") != tcmalloc.get("binary_sha256"):
            raise ReportError("System and TCMalloc binaries are not identical")
        preload_proof = require_mapping(
            tcmalloc.get("preload_proof"), "TCMalloc preload proof"
        )
        if preload_proof.get("success") is not True:
            raise ReportError("TCMalloc preload proof is unsuccessful")
    return [records[variant] for variant in configured_variants]


def first_measurement_commands(
    campaign: Mapping[str, Any],
) -> dict[str, dict[str, list[str]]]:
    commands: dict[str, dict[str, list[str]]] = {}
    for raw_row in require_list(campaign.get("measurements"), "measurements"):
        row = require_mapping(raw_row, "measurement")
        if row.get("warmup") is True:
            continue
        variant = str(row.get("variant", ""))
        if variant in commands:
            continue
        record: dict[str, list[str]] = {}
        for field in ("command", "measured_command"):
            raw_command = row.get(field)
            if isinstance(raw_command, list) and all(
                isinstance(item, str) for item in raw_command
            ):
                record[field] = list(raw_command)
        if record:
            commands[variant] = record
    return commands


def ratio(subject: Mapping[str, Any], reference: Mapping[str, Any], field: str) -> float:
    subject_value = float(subject[field])
    reference_value = float(reference[field])
    if reference_value == 0:
        raise ReportError(f"zero comparator value for {field}")
    return subject_value / reference_value


def comparison_record(
    summaries: Mapping[str, Mapping[str, Any]], subject_name: str, reference_name: str
) -> dict[str, Any]:
    subject = summaries[subject_name]
    reference = summaries[reference_name]
    paired_wall_key = f"paired_wall_ratio_vs_{reference_name}"
    paired_rss_key = f"paired_rss_ratio_vs_{reference_name}"
    wall_ratio = (
        float(subject[paired_wall_key])
        if paired_wall_key in subject
        else ratio(subject, reference, "median_wall_seconds")
    )
    rss_ratio = (
        float(subject[paired_rss_key])
        if paired_rss_key in subject
        else ratio(subject, reference, "median_peak_rss_kib")
    )
    if paired_wall_key in subject:
        throughput_ratio = 1.0 / wall_ratio
        throughput_method = "inverse of paired wall median"
    else:
        throughput_ratio = ratio(
            subject, reference, "median_throughput_per_second"
        )
        throughput_method = "ratio of medians"
    return {
        "subject": subject_name,
        "reference": reference_name,
        "wall_ratio": wall_ratio,
        "wall_delta_percent": (wall_ratio - 1.0) * 100.0,
        "throughput_ratio": throughput_ratio,
        "throughput_delta_percent": (throughput_ratio - 1.0) * 100.0,
        "total_cpu_ratio": ratio(
            subject, reference, "median_total_cpu_seconds"
        ),
        "rss_ratio": rss_ratio,
        "rss_delta_percent": (rss_ratio - 1.0) * 100.0,
        "ratio_method": {
            "wall": "paired median" if paired_wall_key in subject else "ratio of medians",
            "rss": "paired median" if paired_rss_key in subject else "ratio of medians",
            "throughput": throughput_method,
            "total_cpu": "ratio of medians",
        },
    }


def evidence_campaign(campaign: Mapping[str, Any]) -> dict[str, Any]:
    app = str(require_list(campaign.get("apps"), "apps")[0])
    summaries = summary_by_variant(campaign)
    configuration_fields = (
        "schema_version",
        "source",
        "quick",
        "warmups",
        "repetitions",
        "variants",
        "toolchain",
        "implementation_sha256",
        "pass_source_sha256",
        "glibc_rseq_mode",
        "glibc_tunable",
        "performance_stats_enabled",
        "coverage_variant_performance_eligible",
        "run_output_retained",
    )
    configuration = {
        field: copy.deepcopy(campaign[field])
        for field in configuration_fields
        if field in campaign
    }
    workload_map = require_mapping(campaign.get("workloads"), "workloads")
    if app not in workload_map:
        raise ReportError(f"campaign {app} has no workload evidence")
    provenance = {
        "host": copy.deepcopy(campaign["host"]),
        "measurement_affinity": copy.deepcopy(campaign["measurement_affinity"]),
        "tcmalloc_runtime": copy.deepcopy(campaign.get("tcmalloc_runtime")),
        "source": {
            field: copy.deepcopy(campaign[field])
            for field in SOURCE_PROVENANCE_FIELDS
            if field in campaign
        },
        "builds": build_provenance(campaign),
        "measurement_commands": first_measurement_commands(campaign),
    }
    return {
        "app": app,
        "input_path": campaign["_input_path"],
        "input_sha256": campaign["_input_sha256"],
        "configuration": configuration,
        "workload": copy.deepcopy(workload_map[app]),
        "provenance": provenance,
        "summaries": [copy.deepcopy(row) for row in summaries.values()],
        "direct_comparisons": [
            comparison_record(summaries, subject, reference)
            for subject, reference in COMPARISONS
        ],
        # Keep every source row intact so the snapshot remains independently auditable.
        "measurements": copy.deepcopy(campaign["measurements"]),
    }


def build_evidence(campaigns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rendered_campaigns = [evidence_campaign(campaign) for campaign in campaigns]
    aggregates: list[dict[str, Any]] = []
    for subject, reference in COMPARISONS:
        rows = [
            comparison
            for campaign in rendered_campaigns
            for comparison in campaign["direct_comparisons"]
            if comparison["subject"] == subject
            and comparison["reference"] == reference
        ]
        aggregates.append(
            {
                "subject": subject,
                "reference": reference,
                "campaign_count": len(rows),
                "geomean_wall_ratio": math.exp(
                    sum(math.log(float(row["wall_ratio"])) for row in rows)
                    / len(rows)
                ),
                "geomean_throughput_ratio": math.exp(
                    sum(math.log(float(row["throughput_ratio"])) for row in rows)
                    / len(rows)
                ),
                "geomean_total_cpu_ratio": math.exp(
                    sum(math.log(float(row["total_cpu_ratio"])) for row in rows)
                    / len(rows)
                ),
                "geomean_rss_ratio": math.exp(
                    sum(math.log(float(row["rss_ratio"])) for row in rows)
                    / len(rows)
                ),
            }
        )
    return {
        "schema_version": 1,
        "source": "unialloc-realworld-allocator-report",
        "success": True,
        "aggregate_direct_comparisons": aggregates,
        "campaigns": rendered_campaigns,
    }


def variant_label(variant: str) -> str:
    return VARIANT_LABELS.get(variant, variant)


def format_number(value: Any, digits: int = 3) -> str:
    return f"{float(value):.{digits}f}"


def format_percent(value: Any) -> str:
    return f"{float(value):+.2f}%"


def format_rss(kib: Any) -> str:
    return f"{float(kib) / 1024.0:.2f} MiB"


def render_allocator_table(summaries: Sequence[Mapping[str, Any]]) -> list[str]:
    header = (
        "| Allocator | Samples | Wall median | Wall MAD | Throughput | "
        "Total CPU | Peak RSS median [range] | Major/minor faults | "
        "Involuntary/voluntary switches |"
    )
    lines = [
        header,
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        variant = str(row["variant"])
        if row.get("performance_eligible") is False:
            continue
        rss = (
            f"{format_rss(row['median_peak_rss_kib'])} "
            f"[{format_rss(row['min_peak_rss_kib'])} to "
            f"{format_rss(row['max_peak_rss_kib'])}]"
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    variant_label(variant),
                    str(row["sample_count"]),
                    f"{float(row['median_wall_seconds']) * 1000.0:.3f} ms",
                    f"{float(row['wall_mad_seconds']) * 1000.0:.3f} ms",
                    f"{format_number(row['median_throughput_per_second'])} "
                    f"{row['throughput_unit']}",
                    f"{float(row['median_total_cpu_seconds']) * 1000.0:.3f} ms",
                    rss,
                    f"{format_number(row['median_major_page_faults'], 1)}/"
                    f"{format_number(row['median_minor_page_faults'], 1)}",
                    f"{format_number(row['median_involuntary_context_switches'], 1)}/"
                    f"{format_number(row['median_voluntary_context_switches'], 1)}",
                )
            )
            + " |"
        )
    return lines


def render_comparison_table(comparisons: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Subject | Reference | Wall delta | Throughput delta | Total CPU ratio | Peak RSS delta | Ratio method |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for row in comparisons:
        methods = require_mapping(row["ratio_method"], "ratio_method")
        lines.append(
            "| "
            + " | ".join(
                (
                    variant_label(str(row["subject"])),
                    variant_label(str(row["reference"])),
                    format_percent(row["wall_delta_percent"]),
                    format_percent(row["throughput_delta_percent"]),
                    f"{float(row['total_cpu_ratio']):.4f}x",
                    format_percent(row["rss_delta_percent"]),
                    f"wall: {methods['wall']}; throughput: {methods['throughput']}; "
                    f"CPU: {methods['total_cpu']}; RSS: {methods['rss']}",
                )
            )
            + " |"
        )
    return lines


def json_block(value: Any) -> list[str]:
    return ["```json", json.dumps(value, indent=2, sort_keys=True), "```"]


def reproduction_command(campaign: Mapping[str, Any]) -> str:
    configuration = require_mapping(campaign["configuration"], "configuration")
    provenance = require_mapping(campaign["provenance"], "provenance")
    affinity = require_mapping(
        provenance["measurement_affinity"], "measurement_affinity"
    )
    tcmalloc = require_mapping(provenance["tcmalloc_runtime"], "tcmalloc_runtime")
    builds = [
        require_mapping(row, "build")
        for row in require_list(provenance["builds"], "builds")
    ]
    jobs = None
    for build in builds:
        command = build.get("command")
        if not isinstance(command, list) or "--jobs" not in command:
            continue
        index = command.index("--jobs")
        if index + 1 < len(command):
            jobs = str(command[index + 1])
            break
    command = [
        "python3",
        "evaluation/scripts/realworld_type_isolation_matrix.py",
        "--apps",
        str(campaign["app"]),
        "--variants",
        ",".join(str(value) for value in configuration["variants"]),
        "--quick" if configuration.get("quick") else "--full",
        "--warmups",
        str(configuration["warmups"]),
        "--repetitions",
        str(configuration["repetitions"]),
        "--toolchain",
        str(configuration["toolchain"]),
    ]
    workload = require_mapping(campaign["workload"], "workload")
    path_repetitions = workload.get("path_repetitions")
    if campaign["app"] == "ripgrep" and path_repetitions is not None:
        command.extend(["--ripgrep-path-repetitions", str(path_repetitions)])
    if campaign["app"] == "fd" and path_repetitions is not None:
        command.extend(["--fd-path-repetitions", str(path_repetitions)])
    if affinity.get("cpu_list") is not None:
        command.extend(["--cpu-list", str(affinity["cpu_list"])])
    if affinity.get("numa_node") is not None:
        command.extend(["--numa-node", str(affinity["numa_node"])])
    if jobs is not None:
        command.extend(["--jobs", jobs])
    command.extend(["--tcmalloc-library", str(tcmalloc["library"])])
    if configuration.get("run_output_retained") is False:
        command.append("--discard-run-output")
    raw_dir = pathlib.Path(str(campaign["input_path"])).parent
    command.extend(["--raw-dir", str(raw_dir)])
    return shlex.join(command)


def render_markdown(evidence: Mapping[str, Any], title: str) -> str:
    lines = [
        f"# {title}",
        "",
        "Lower wall time, total CPU, peak RSS, faults, and context switches are better. "
        "Higher throughput is better. Performance rows exclude the statistics-enabled "
        "coverage build.",
        "",
        "## Interpretation boundaries",
        "",
        "- These are warm-cache, CPU- and NUMA-pinned measurements on a shared host.",
        "- Peak RSS is GNU time `%M`, covering the complete measured process.",
        "- TCMalloc uses an identical Rust System binary plus gperftools `LD_PRELOAD`; "
        "mimalloc uses its native Rust `GlobalAlloc` wrapper and a statically linked core.",
        "- The TCMalloc proof verifies that the exact shared library is mapped; exported "
        "allocator-symbol validation remains a separate manual check.",
        "- The runner inherits its parent environment; same-host reruns should start from "
        "a clean allocator environment.",
        "- Cross-application geometric means are directional summaries; per-application "
        "absolute time and RSS remain the primary evidence.",
        "",
        "## Cross-application comparison",
        "",
        "| Subject | Reference | Campaigns | Wall geomean | Throughput geomean | Total CPU geomean | Peak RSS geomean |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for raw_row in require_list(
        evidence.get("aggregate_direct_comparisons"),
        "aggregate_direct_comparisons",
    ):
        row = require_mapping(raw_row, "aggregate comparison")
        lines.append(
            "| "
            + " | ".join(
                (
                    variant_label(str(row["subject"])),
                    variant_label(str(row["reference"])),
                    str(row["campaign_count"]),
                    f"{float(row['geomean_wall_ratio']):.4f}x",
                    f"{float(row['geomean_throughput_ratio']):.4f}x",
                    f"{float(row['geomean_total_cpu_ratio']):.4f}x",
                    f"{float(row['geomean_rss_ratio']):.4f}x",
                )
            )
            + " |"
        )
    aggregate_rows = {
        (str(require_mapping(row, "aggregate comparison")["subject"]),
         str(require_mapping(row, "aggregate comparison")["reference"])):
        require_mapping(row, "aggregate comparison")
        for row in require_list(
            evidence.get("aggregate_direct_comparisons"),
            "aggregate_direct_comparisons",
        )
    }
    uni_mi = aggregate_rows[("unialloc", "mimalloc")]
    uni_tc = aggregate_rows[("unialloc", "tcmalloc")]
    typeiso = aggregate_rows[("typeiso_perf", "typed_plain")]
    lines.extend(
        (
            "",
            "## Headline",
            "",
            f"- UniAlloc's cross-workload wall-time geomean is "
            f"{format_percent((float(uni_mi['geomean_wall_ratio']) - 1.0) * 100.0)} "
            f"versus mimalloc and "
            f"{format_percent((float(uni_tc['geomean_wall_ratio']) - 1.0) * 100.0)} "
            f"versus TCMalloc.",
            f"- UniAlloc's peak-RSS geomean is "
            f"{format_percent((float(uni_mi['geomean_rss_ratio']) - 1.0) * 100.0)} "
            f"versus mimalloc and "
            f"{format_percent((float(uni_tc['geomean_rss_ratio']) - 1.0) * 100.0)} "
            f"versus TCMalloc.",
            f"- Type Isolation adds "
            f"{format_percent((float(typeiso['geomean_wall_ratio']) - 1.0) * 100.0)} "
            f"wall time and "
            f"{format_percent((float(typeiso['geomean_rss_ratio']) - 1.0) * 100.0)} "
            f"peak RSS over typed plain across these workloads.",
            "",
        )
    )
    campaign_rows = [
        require_mapping(row, "campaign")
        for row in require_list(evidence.get("campaigns"), "campaigns")
    ]
    lines.extend(
        (
            "## Same-host reproduction",
            "",
            "These commands reuse the audited TCMalloc shared library and pinned "
            "toolchain at their recorded absolute paths on this host.",
            "",
        )
    )
    for campaign in campaign_rows:
        lines.extend(
            (
                f"### {campaign['app']}",
                "",
                "```sh",
                reproduction_command(campaign),
                "```",
                "",
            )
        )
    raw_gaps = [
        (campaign, require_mapping(comparison, "comparison"))
        for campaign in campaign_rows
        for comparison in require_list(
            campaign["direct_comparisons"], "direct_comparisons"
        )
        if require_mapping(comparison, "comparison")["subject"] == "unialloc"
    ]
    raw_campaign, raw_gap = max(
        raw_gaps, key=lambda item: float(item[1]["wall_ratio"])
    )
    raw_summaries = {
        str(require_mapping(row, "summary")["variant"]): require_mapping(row, "summary")
        for row in require_list(raw_campaign["summaries"], "summaries")
    }
    raw_reference = str(raw_gap["reference"])
    typeiso_gaps = [
        (campaign, require_mapping(comparison, "comparison"))
        for campaign in campaign_rows
        for comparison in require_list(
            campaign["direct_comparisons"], "direct_comparisons"
        )
        if require_mapping(comparison, "comparison")["subject"] == "typeiso_perf"
    ]
    typeiso_campaign, typeiso_gap = max(
        typeiso_gaps, key=lambda item: float(item[1]["wall_ratio"])
    )
    typeiso_summaries = {
        str(require_mapping(row, "summary")["variant"]): require_mapping(row, "summary")
        for row in require_list(typeiso_campaign["summaries"], "summaries")
    }
    typed_vs_raw = ratio(
        typeiso_summaries["typed_plain"],
        typeiso_summaries["unialloc"],
        "median_wall_seconds",
    )
    lines.extend(
        (
            "## Profile priority",
            "",
            f"- The largest raw UniAlloc gap is `{raw_campaign['app']}` versus "
            f"{variant_label(raw_reference)}: "
            f"{format_percent(raw_gap['wall_delta_percent'])} wall time. UniAlloc "
            f"records {format_number(raw_summaries['unialloc']['median_minor_page_faults'], 0)} "
            f"median minor faults versus "
            f"{format_number(raw_summaries[raw_reference]['median_minor_page_faults'], 0)}.",
            f"- The largest incremental Type Isolation gap is `{typeiso_campaign['app']}`: "
            f"{format_percent(typeiso_gap['wall_delta_percent'])} over typed plain. "
            f"Typed plain is {typed_vs_raw:.3f}x raw UniAlloc in that workload, so the "
            f"base typed compiler/runtime path dominates the policy-only increment.",
            "",
        )
    )
    for raw_campaign in require_list(evidence.get("campaigns"), "campaigns"):
        campaign = require_mapping(raw_campaign, "campaign")
        app = str(campaign["app"])
        configuration = require_mapping(campaign["configuration"], "configuration")
        provenance = require_mapping(campaign["provenance"], "provenance")
        lines.extend(
            (
                f"## {app}",
                "",
                f"- Input evidence: `{campaign['input_path']}`",
                f"- Input SHA-256: `{campaign['input_sha256']}`",
                f"- Toolchain: `{configuration.get('toolchain', '<unknown>')}`",
                f"- Mode: `{'quick' if configuration.get('quick') else 'full'}`; "
                f"warmups: `{configuration.get('warmups')}`; repetitions: "
                f"`{configuration.get('repetitions')}`",
                f"- glibc rseq mode: `{configuration.get('glibc_rseq_mode', '<unknown>')}`",
                f"- Implementation SHA-256: `{configuration.get('implementation_sha256', '<unknown>')}`",
                "",
                "### Performance and memory",
                "",
            )
        )
        summaries = [
            require_mapping(row, "summary")
            for row in require_list(campaign["summaries"], "summaries")
        ]
        lines.extend(render_allocator_table(summaries))
        lines.extend(("", "### Direct comparisons", ""))
        comparisons = [
            require_mapping(row, "comparison")
            for row in require_list(campaign["direct_comparisons"], "direct_comparisons")
        ]
        lines.extend(render_comparison_table(comparisons))
        lines.extend(("", "### Workload", ""))
        lines.extend(json_block(campaign["workload"]))
        host = require_mapping(provenance["host"], "host")
        affinity = require_mapping(
            provenance["measurement_affinity"], "measurement_affinity"
        )
        tcmalloc = require_mapping(
            provenance["tcmalloc_runtime"], "tcmalloc_runtime"
        )
        source = require_mapping(provenance["source"], "source provenance")
        if app == "fd":
            input_record = require_mapping(source.get("fd_tree"), "fd tree")
        elif app == "ripgrep":
            input_record = require_mapping(source.get("corpus"), "ripgrep corpus")
        else:
            input_record = require_mapping(campaign["workload"], "workload")
        input_tree_sha256 = input_record.get(
            "tree_sha256", input_record.get("input_sha256")
        )
        if not isinstance(input_tree_sha256, str) or not input_tree_sha256:
            raise ReportError(f"campaign {app} has no workload input SHA-256")
        builds = {
            str(require_mapping(row, "build")["variant"]): require_mapping(row, "build")
            for row in require_list(provenance["builds"], "builds")
        }
        mimalloc = require_mapping(
            builds["mimalloc"].get("allocator_provenance"),
            "mimalloc allocator provenance",
        )
        wrapper = require_mapping(mimalloc["wrapper_package"], "mimalloc wrapper")
        sys_package = require_mapping(mimalloc["sys_package"], "mimalloc sys")
        output_hashes = sorted(
            {
                str(require_mapping(row, "measurement")["output_sha256"])
                for row in require_list(campaign["measurements"], "measurements")
            }
        )
        lines.extend(
            (
                "",
                "### Provenance and controls",
                "",
                f"- Host: `{host.get('cpu_model')}`; kernel `{host.get('kernel')}`; "
                f"initial load average `{host.get('load_average')}`.",
                f"- Affinity: physical CPU `{affinity.get('cpu_list')}`; NUMA node "
                f"`{affinity.get('numa_node')}`.",
                f"- TCMalloc: `{tcmalloc.get('label')}` via "
                f"`{tcmalloc.get('activation')}`; library SHA-256 "
                f"`{tcmalloc.get('library_sha256')}`.",
                f"- mimalloc: wrapper `{wrapper.get('version')}`; sys crate "
                f"`{sys_package.get('version')}`; core generation "
                f"`{mimalloc.get('core_generation')}` version number "
                f"`{mimalloc.get('core_version_number')}`; secure mode "
                f"`{mimalloc.get('secure_feature_enabled')}`.",
                f"- Workload input SHA-256: `{input_tree_sha256}`.",
                f"- Output-equivalence SHA-256: `{', '.join(output_hashes)}`.",
                "- Full build records, loader proofs, commands, summaries, and every "
                "measurement row are preserved in the companion JSON evidence.",
                "",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def write_text_atomic(path: pathlib.Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    seen: set[str] = set()
    campaigns: list[dict[str, Any]] = []
    for app, path in args.campaign:
        if app in seen:
            raise ReportError(f"duplicate campaign app: {app}")
        seen.add(app)
        campaigns.append(load_campaign(app, path))
    evidence = build_evidence(campaigns)
    markdown = render_markdown(evidence, args.title)
    write_text_atomic(args.json_out, json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    write_text_atomic(args.markdown_out, markdown)
    print(
        json.dumps(
            {
                "success": True,
                "apps": [campaign["app"] for campaign in evidence["campaigns"]],
                "json": str(args.json_out.resolve()),
                "markdown": str(args.markdown_out.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
