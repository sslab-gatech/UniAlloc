use std::env;
use std::fs;
use std::path::PathBuf;

use oxipng::{optimize_from_memory, Options};
use unialloc::{
    semantic_stats_recording_disable, semantic_stats_recording_enable, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_stats_recording_disable,
    semantic_type_stats_recording_enable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOC: UniAlloc = UniAlloc;

fn json_escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
}

fn main() {
    let input = env::args_os()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            eprintln!("usage: external-oxipng-unialloc <input.png>");
            std::process::exit(2);
        });
    let input_bytes = fs::read(&input).unwrap_or_else(|err| {
        eprintln!("failed to read {}: {err}", input.display());
        std::process::exit(2);
    });

    semantic_stats_reset();
    semantic_stats_recording_enable();
    semantic_type_stats_recording_enable();

    let output = optimize_from_memory(&input_bytes, &Options::default()).unwrap_or_else(|err| {
        eprintln!("oxipng optimize_from_memory failed: {err}");
        std::process::exit(1);
    });
    let output_len = output.len();
    let output_checksum = output
        .iter()
        .fold(0u64, |acc, byte| acc.wrapping_mul(16777619) ^ u64::from(*byte));

    let stats = semantic_stats_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 64];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    println!(
        "{{\"source\":\"external-oxipng-unialloc\",\"input\":\"{}\",\"input_bytes\":{},\"output_bytes\":{},\"output_checksum\":{},\"total_allocations\":{},\"typed_allocations\":{},\"fallback_allocations\":{},\"typed_deallocations\":{},\"fallback_deallocations\":{},\"typed_cache_hits\":{},\"typed_cache_inserts\":{},\"typed_cache_bypasses\":{},\"coverage_basis_points\":{},\"type_stats_rows\":{},\"type_stats_dropped_events\":{},\"type_isolation_inline_occupied\":{},\"type_isolation_occupied_slots\":{},\"type_isolation_occupied_entries\":{},\"type_isolation_corrupt_slots\":{}}}",
        json_escape(&input.display().to_string()),
        input_bytes.len(),
        output_len,
        output_checksum,
        stats.total_allocations,
        stats.typed_allocations,
        stats.fallback_allocations,
        stats.typed_deallocations,
        stats.fallback_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.typed_cache_bypasses,
        stats.coverage_basis_points,
        row_count,
        stats.semantic_type_stats_dropped_events,
        side_cache.inline_occupied,
        side_cache.occupied_slots,
        side_cache.occupied_entries,
        side_cache.corrupt_slots,
    );

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
}
