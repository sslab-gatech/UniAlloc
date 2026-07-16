#!/usr/bin/env python3
"""Render the source-bound mixed-filler fastpath backup slide as SVG."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUMMARY = (
    REPOSITORY_ROOT
    / "docs"
    / "evidence"
    / "lifetime-resident-index-20260715"
    / "mixed-filler-fastpath-swc-quick-summary.json"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "docs"
    / "figures"
    / "lifetime-resident-index-20260715"
    / "mixed-filler-fastpath-evidence-slide.svg"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_summary(path: Path) -> dict[str, Any]:
    summary = json.loads(path.read_text(encoding="utf-8"))
    if summary.get("schema_version") != 2:
        raise ValueError("expected compact mixed-filler summary schema version 2")
    if summary.get("claim_grade") is not False:
        raise ValueError("diagnostic slide requires claim_grade=false")
    if summary.get("presentation_claim_eligible") is not False:
        raise ValueError("diagnostic slide requires presentation_claim_eligible=false")
    if summary.get("performance_claim_eligible") is not False:
        raise ValueError("diagnostic slide requires performance_claim_eligible=false")
    if summary.get("physical_backing_gate", {}).get("verified") is not True:
        raise ValueError("physical-backing gate must be verified")
    comparison = summary.get("comparison_to_prior_scan_bound_prototype", {})
    if comparison.get("same_fixed_work_and_routing_verified") is not True:
        raise ValueError("prior/current scan comparison must verify fixed work and routing")
    if comparison.get("separate_allocator_binaries") is not True:
        raise ValueError("prior/current scan comparison must record separate binaries")
    if comparison.get("prior_binary_sha256") == comparison.get(
        "current_binary_sha256"
    ):
        raise ValueError("prior/current scan comparison requires distinct binaries")
    workload = summary.get("workload", {})
    if workload.get("output_identity_equal") is not True:
        raise ValueError("diagnostic slide requires equal output identities")
    rounds = workload.get("rounds_per_arm")
    routed = {
        arm["median"]["routed_allocations"] for arm in summary.get("arms", {}).values()
    }
    samples = {arm["sample_count"] for arm in summary.get("arms", {}).values()}
    if len(routed) != 1 or len(samples) != 1 or samples != {rounds}:
        raise ValueError("four arms must share routing and sample counts")
    return summary


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_svg(summary: dict[str, Any], summary_sha256: str) -> str:
    arms = summary["arms"]
    decision = summary["decision"]
    mixed_vs_legacy = summary["paired_contrasts"]["mixed-vs-legacy-policy-2"]
    backing = summary["paired_contrasts"]["policy-2-vs-policy-1-mixed"]
    legacy = arms["legacy-policy-2"]["median"]
    mixed = arms["mixed-policy-2"]["median"]
    workload = summary["workload"]
    artifact = summary["artifact"]
    gate = summary["physical_backing_gate"]
    prior_comparison = summary["comparison_to_prior_scan_bound_prototype"]

    legacy_time = mixed_vs_legacy["metrics"]["operation_seconds"]
    backing_time = backing["metrics"]["operation_seconds"]
    legacy_rss = mixed_vs_legacy["metrics"]["peak_rss_kib"]

    def forest_x(value: float) -> float:
        # Shared -1% to +3% axis in the timing panel.
        return 1115.0 + ((value + 1.0) / 4.0) * 355.0

    legacy_point = float(legacy_time["point_estimate_median_paired_percent"])
    legacy_low = float(legacy_time["ci_low"])
    legacy_high = float(legacy_time["ci_high"])
    backing_point = float(backing_time["point_estimate_median_paired_percent"])
    backing_low = float(backing_time["ci_low"])
    backing_high = float(backing_time["ci_high"])
    routed_allocations = int(mixed["routed_allocations"])
    cache_hits = int(mixed["mixed_hot_region_cache_hits"])
    cache_hit_percent = 100.0 * float(decision["mixed_cache_lookup_hit_rate"])
    legacy_searches = int(legacy["available_list_search_attempts"])
    mixed_searches = int(mixed["available_list_search_attempts"])
    search_reduction = float(decision["available_list_search_reduction_percent"])
    previous_scan_selections = int(decision["previous_scan_selections"])
    current_scan_selections = int(decision["current_scan_selections"])
    scan_reduction = float(decision["scan_selection_reduction_percent"])
    current_scan_attempts = int(mixed["mixed_filler_scan_attempts"])
    legacy_extents = int(legacy["peak_extents"])
    mixed_extents = int(mixed["peak_extents"])
    extent_reduction = float(decision["extent_reduction_percent"])
    legacy_thp_mib = int(legacy["anon_hugepages_kib"] / 1024)
    mixed_thp_mib = int(mixed["anon_hugepages_kib"] / 1024)
    ordinary_backing_kib = 0
    mixed_thp_backing_kib = int(gate["thp_anon_hugepages_kib_each_sample"])

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900" role="img" aria-labelledby="title description">
  <title id="title">Cache-first indexing cuts scan selections 99.854 percent</title>
  <desc id="description">A source-bound diagnostic slide showing packing, fastpath, operation-time, and peak-RSS evidence from the SWC fixed-work campaign.</desc>
  <rect width="1600" height="900" fill="#FFFFFF"/>
  <style>
    text {{ font-family: Aptos, Arial, sans-serif; fill: #17181A; }}
    .title {{ font-size: 42px; font-weight: 700; }}
    .subtitle {{ font-size: 20px; fill: #62646B; }}
    .panel-title {{ font-size: 23px; font-weight: 700; }}
    .label {{ font-size: 17px; font-weight: 600; }}
    .metric {{ font-size: 38px; font-weight: 700; }}
    .detail {{ font-size: 16px; fill: #47484F; }}
    .small {{ font-size: 14px; fill: #62646B; }}
    .badge {{ font-size: 14px; font-weight: 700; letter-spacing: 1px; }}
  </style>

  <text id="slide-title" class="title" x="70" y="72">Cache-first indexing cuts scan selections {scan_reduction:.3f}%.</text>
  <text class="subtitle" x="70" y="108">Mixed packing keeps {mixed_extents} extents; current four-arm contrasts share one binary and {routed_allocations:,} routes/process.</text>
  <rect x="1230" y="42" width="300" height="42" rx="21" fill="#FDE8DE"/>
  <text class="badge" x="1380" y="68" text-anchor="middle" fill="#9D3F1F">CURRENT DIAGNOSTIC · NOT CLAIM-GRADE</text>

  <!-- Packing panel -->
  <rect x="60" y="145" width="465" height="575" rx="18" fill="#F7F8FA" stroke="#DDDEE1"/>
  <text class="panel-title" x="90" y="190">1  PACK DENSELY</text>
  <text class="detail" x="90" y="220">Legacy geometry → heterogeneous 64 KiB regions</text>
  <text class="label" x="90" y="275">Peak extents</text>
  <rect x="215" y="250" width="242" height="30" rx="6" fill="#BCBEC4"/>
  <rect x="215" y="295" width="66" height="30" rx="6" fill="#5477C4"/>
  <text class="detail" x="465" y="272" text-anchor="end">{legacy_extents}</text>
  <text class="detail" x="292" y="317">{mixed_extents}</text>
  <text class="metric" x="90" y="390">{legacy_extents} → {mixed_extents}</text>
  <text class="detail" x="90" y="420">{extent_reduction:.1f}% fewer simultaneously live extents</text>

  <text class="label" x="90" y="485">Physical THP footprint</text>
  <rect x="90" y="510" width="340" height="34" rx="6" fill="#A3D576"/>
  <rect x="90" y="560" width="93" height="34" rx="6" fill="#2E4780"/>
  <text class="detail" x="440" y="534" text-anchor="end">{legacy_thp_mib} MiB</text>
  <text class="detail" x="193" y="584">{mixed_thp_mib} MiB</text>
  <text class="detail" x="90" y="635">Every ordinary sample: {ordinary_backing_kib:,} KiB</text>
  <text class="detail" x="90" y="662">Every mixed-THP sample: {mixed_thp_backing_kib:,} KiB</text>

  <!-- Fastpath panel -->
  <rect x="555" y="145" width="465" height="575" rx="18" fill="#FFF7F2" stroke="#F0C8B5"/>
  <text class="panel-title" x="585" y="190">2  INDEX THE HOT REGION</text>
  <text class="detail" x="585" y="220">Prior scan prototype → cache-first snapshot</text>
  <text class="small" x="585" y="245">Same fixed SWC work/routing; separate allocator binaries</text>
  <text class="metric" x="585" y="300">{cache_hit_percent:.2f}%</text>
  <text class="detail" x="585" y="330">{cache_hits:,} / {routed_allocations:,} exact cache hits</text>

  <text class="label" x="585" y="390">Available-list searches</text>
  <text class="metric" x="585" y="440">{legacy_searches:,} → {mixed_searches:,}</text>
  <text class="detail" x="585" y="468">{search_reduction:.2f}% fewer searches</text>

  <text class="label" x="585" y="530">Descriptor-scan selections</text>
  <text class="metric" x="585" y="580">{previous_scan_selections:,} → {current_scan_selections:,}</text>
  <text class="detail" x="585" y="608">{scan_reduction:.3f}% fewer selections</text>
  <text class="detail" x="585" y="660">Remaining scan attempts: {current_scan_attempts:,}</text>
  <text class="small" x="585" y="688">Routed deallocation still takes one arena slow-path lock/object.</text>

  <!-- Outcome panel -->
  <rect x="1050" y="145" width="490" height="575" rx="18" fill="#F5F8F2" stroke="#C9DDB8"/>
  <text class="panel-title" x="1080" y="190">3  MEASURE TIME AND MEMORY</text>
  <text class="detail" x="1080" y="220">Paired percent saving; positive means faster/lower</text>

  <line x1="1115" y1="290" x2="1470" y2="290" stroke="#BCBEC4" stroke-width="2"/>
  <line x1="{forest_x(0)}" y1="260" x2="{forest_x(0)}" y2="420" stroke="#62646B" stroke-width="2" stroke-dasharray="5 5"/>
  <text class="small" x="1115" y="315">-1%</text><text class="small" x="{forest_x(0)}" y="315" text-anchor="middle">0%</text><text class="small" x="1470" y="315" text-anchor="end">+3%</text>

  <text class="label" x="1080" y="355">Mixed vs legacy THP</text>
  <line x1="{forest_x(legacy_low)}" y1="385" x2="{forest_x(legacy_high)}" y2="385" stroke="#2E4780" stroke-width="7" stroke-linecap="round"/>
  <circle cx="{forest_x(legacy_point)}" cy="385" r="9" fill="#2E4780"/>
  <text class="detail" x="1080" y="420">{legacy_point:+.3f}%  [95% CI {legacy_low:+.3f}%, {legacy_high:+.3f}%]</text>

  <text class="label" x="1080" y="470">Mixed THP vs mixed ordinary</text>
  <line x1="{forest_x(backing_low)}" y1="500" x2="{forest_x(backing_high)}" y2="500" stroke="#67A83E" stroke-width="7" stroke-linecap="round"/>
  <circle cx="{forest_x(backing_point)}" cy="500" r="9" fill="#67A83E"/>
  <text class="detail" x="1080" y="535">+{backing_point:.3f}%  [95% CI +{backing_low:.3f}%, +{backing_high:.3f}%] · 8/8</text>

  <text class="label" x="1080" y="590">Peak RSS arm medians: legacy THP → mixed THP</text>
  <text class="metric" x="1080" y="640">{int(legacy['peak_rss_kib']):,} → {int(mixed['peak_rss_kib']):,} KiB</text>
  <text class="detail" x="1080" y="675">Paired median saving +{float(legacy_rss['point_estimate_median_paired_percent']):.3f}% · 95% CI [{float(legacy_rss['ci_low']):.3f}%, {float(legacy_rss['ci_high']):.3f}%] · 8/8</text>

  <rect x="60" y="750" width="1480" height="95" rx="14" fill="#F0F1F2"/>
  <text class="detail" x="85" y="785">Boundary: SWC fixed work; N={int(workload['rounds_per_arm'])} paired blocks; {int(workload['total_fresh_processes'])} fresh processes; CPU {int(workload['cpu'])}; zero app warmup; no Criterion.</text>
  <text class="detail" x="85" y="815">Dirty working-tree snapshot; performance_claim_eligible=false; presentation_claim_eligible=false. Use as mechanism evidence pending a clean committed rerun.</text>
  <text class="small" x="85" y="838">Raw {esc(artifact['sha256'][:16])}… · compact {esc(summary_sha256[:16])}… · output identity equal · separate allocator binaries {esc(prior_comparison['prior_binary_sha256'][:8])}…/{esc(prior_comparison['current_binary_sha256'][:8])}…</text>
  <text class="small" x="1520" y="838" text-anchor="end">B13</text>
</svg>
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = load_summary(args.summary)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_svg(summary, sha256_file(args.summary)), encoding="utf-8"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
