extern crate alloc;

use alloc::boxed::Box;
use alloc::collections::{BTreeMap, BinaryHeap, VecDeque};
use alloc::vec::Vec;
use std::env;
use std::fmt::Write;
use std::process;
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::Instant;
use unialloc::{
    platform_allocator_backend, platform_system_backend, platform_thread_local_backend,
    platform_tls_key_ready, platform_tls_save_failure_count, semantic_stats_recording_disable,
    semantic_stats_recording_enabled, semantic_stats_reset, semantic_stats_snapshot,
    SemanticStatsSnapshot, UniAlloc,
};

#[global_allocator]
static A: UniAlloc = UniAlloc;

const MIX_A: usize = 0x9e37_79b9_7f4a_7c15u64 as usize;
const MIX_B: usize = 0xbf58_476d_1ce4_e5b9u64 as usize;
const MIX_C: usize = 0x94d0_49bb_1331_11ebu64 as usize;
const MAX_THREADS: usize = 1024;
const MAX_WORKLOAD_ALLOCATION_LEN: usize = 1 << 24;

const USAGE: &str = "usage: platform_allocator_workload [--threads N] [--iters N] [--max-len N]";

#[derive(Clone, Copy)]
struct Config {
    threads: usize,
    iters: usize,
    max_len: usize,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            threads: 8,
            iters: 512,
            max_len: 4096,
        }
    }
}

enum ConfigAction {
    Run(Config),
    Help,
}

fn parse_config() -> Result<ConfigAction, String> {
    parse_config_from(env::args().skip(1))
}

fn parse_config_from(args: impl IntoIterator<Item = String>) -> Result<ConfigAction, String> {
    let mut cfg = Config::default();
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--threads" => cfg.threads = parse_next(&mut args, "threads")?,
            "--iters" => cfg.iters = parse_next(&mut args, "iters")?,
            "--max-len" => cfg.max_len = parse_next(&mut args, "max-len")?,
            "--help" | "-h" => return Ok(ConfigAction::Help),
            other => return Err(format!("unknown argument: {}", other)),
        }
    }
    validate_config(cfg)?;
    Ok(ConfigAction::Run(cfg))
}

fn parse_next(args: &mut impl Iterator<Item = String>, name: &str) -> Result<usize, String> {
    args.next()
        .ok_or_else(|| format!("missing value for --{}", name))?
        .parse::<usize>()
        .map_err(|_| format!("invalid integer for --{}", name))
}

fn validate_config(cfg: Config) -> Result<(), String> {
    if cfg.threads == 0 {
        return Err("threads must be non-zero".to_string());
    }
    if cfg.threads > MAX_THREADS {
        return Err(format!("threads must not exceed {}", MAX_THREADS));
    }
    if cfg.iters == 0 {
        return Err("iters must be non-zero".to_string());
    }
    if cfg.max_len < 64 {
        return Err("max-len must be at least 64".to_string());
    }
    if cfg.max_len > MAX_WORKLOAD_ALLOCATION_LEN {
        return Err(format!(
            "max-len must not exceed {}",
            MAX_WORKLOAD_ALLOCATION_LEN
        ));
    }
    Ok(())
}

fn worker(thread_id: usize, cfg: Config, barrier: Arc<Barrier>) -> usize {
    barrier.wait();

    let mut checksum = (thread_id + 1).wrapping_mul(MIX_A);
    let mut deque = VecDeque::with_capacity(32);
    let mut heap = BinaryHeap::new();
    let mut map = BTreeMap::new();

    for iter in 0..cfg.iters {
        let len = 1 + ((thread_id * 131 + iter * 17) % cfg.max_len);
        let byte = ((thread_id * 31) ^ iter) as u8;
        let mut data = Vec::with_capacity(len);
        data.resize(len, byte);
        checksum = checksum.wrapping_add(data[0] as usize);
        checksum = checksum.wrapping_add(data[len - 1] as usize);

        deque.push_back(Box::new([
            thread_id as u64,
            iter as u64,
            len as u64,
            checksum as u64,
        ]));
        if deque.len() > 64 {
            if let Some(old) = deque.pop_front() {
                checksum ^= old[0] as usize ^ old[1] as usize ^ old[2] as usize;
            }
        }

        heap.push(len ^ checksum);
        if heap.len() > 128 {
            checksum ^= heap.pop().unwrap_or_default();
        }

        map.insert((thread_id, iter), len ^ checksum);
        if map.len() > 128 {
            let remove_key = (thread_id, iter - 127);
            if let Some(value) = map.remove(&remove_key) {
                checksum = checksum.wrapping_add(value);
            }
        }
    }

    checksum ^= deque.len() ^ heap.len() ^ map.len();
    checksum
}

fn mix_worker_checksum(acc: usize, worker_checksum: usize, worker_index: usize) -> usize {
    let shift = ((worker_index * 7) % usize::BITS as usize) as u32;
    let ordinal = worker_index + 1;
    let keyed = worker_checksum
        .rotate_left(shift)
        .wrapping_add(ordinal.wrapping_mul(MIX_B));
    acc.rotate_left(13).wrapping_add(keyed) ^ keyed.rotate_right(7)
}

fn aggregate_worker_checksums(
    cfg: Config,
    worker_checksums: impl IntoIterator<Item = usize>,
) -> usize {
    let mut checksum = MIX_C
        ^ cfg.threads.wrapping_mul(MIX_A)
        ^ cfg.iters.rotate_left(11)
        ^ cfg.max_len.rotate_left(23);
    for (idx, worker_checksum) in worker_checksums.into_iter().enumerate() {
        checksum = mix_worker_checksum(checksum, worker_checksum, idx);
    }
    checksum ^ checksum.rotate_right(17)
}

struct WorkloadReport {
    cfg: Config,
    checksum: usize,
    duration_ns: u128,
    allocator_stats: SemanticStatsSnapshot,
    allocator_stats_recording_active: bool,
}

fn run_workload(cfg: Config) -> Result<WorkloadReport, String> {
    // C007 platform-retargeting evidence should prove that the target workload
    // actually exercised UniAlloc, not merely that a binary linked a symbol with
    // the expected name.  The stats feature keeps this opt-in for evidence runs:
    // when the feature is absent the fields below intentionally stay zero and
    // the evaluator rejects the runtime event as too weak for claim evidence.
    semantic_stats_reset();
    let start = Instant::now();
    let barrier = Arc::new(Barrier::new(cfg.threads));
    let mut handles = Vec::with_capacity(cfg.threads);

    for thread_id in 0..cfg.threads {
        let barrier = Arc::clone(&barrier);
        let handle = thread::Builder::new()
            .name(format!("unialloc-platform-worker-{}", thread_id))
            .spawn(move || worker(thread_id, cfg, barrier))
            .map_err(|err| format!("failed to spawn worker {}: {}", thread_id, err))?;
        handles.push(handle);
    }

    let mut worker_checksums = Vec::with_capacity(cfg.threads);
    for (thread_id, handle) in handles.into_iter().enumerate() {
        match handle.join() {
            Ok(checksum) => worker_checksums.push(checksum),
            Err(_) => return Err(format!("allocator worker {} panicked", thread_id)),
        }
    }
    let checksum = aggregate_worker_checksums(cfg, worker_checksums);
    if checksum == 0 {
        return Err("workload checksum should not be zero".to_string());
    }

    let elapsed = start.elapsed();
    let allocator_stats = semantic_stats_snapshot();
    let allocator_stats_recording_active = semantic_stats_recording_enabled();
    semantic_stats_recording_disable();
    Ok(WorkloadReport {
        cfg,
        checksum,
        duration_ns: elapsed.as_nanos(),
        allocator_stats,
        allocator_stats_recording_active,
    })
}

fn json_escape(input: &str) -> String {
    let mut escaped = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            ch if ch.is_control() => {
                let _ = write!(&mut escaped, "\\u{:04x}", ch as u32);
            }
            ch => escaped.push(ch),
        }
    }
    escaped
}

fn config_number_json(cfg: Option<Config>, field: fn(Config) -> usize) -> String {
    cfg.map(field)
        .map(|value| value.to_string())
        .unwrap_or_else(|| "null".to_string())
}

fn bool_json(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn platform_runtime_metadata_json() -> String {
    format!(
        concat!(
            "\"allocator\":\"UniAlloc\",",
            "\"global_allocator\":\"UniAlloc\",",
            "\"global_allocator_active\":true,",
            "\"allocator_backend\":\"{}\",",
            "\"system_backend\":\"{}\",",
            "\"thread_local_backend\":\"{}\",",
            "\"thread_local_key_ready\":{},",
            "\"thread_local_save_failures\":{},",
            "\"target_os\":\"{}\",",
            "\"target_family\":\"{}\",",
            "\"target_arch\":\"{}\""
        ),
        json_escape(platform_allocator_backend()),
        json_escape(platform_system_backend()),
        json_escape(platform_thread_local_backend()),
        bool_json(platform_tls_key_ready()),
        platform_tls_save_failure_count(),
        json_escape(env::consts::OS),
        json_escape(env::consts::FAMILY),
        json_escape(env::consts::ARCH)
    )
}

fn allocator_stats_json(report: &WorkloadReport) -> String {
    let stats = report.allocator_stats;
    let classified_deallocations = stats
        .typed_deallocations
        .saturating_add(stats.fallback_deallocations);
    let allocation_counters_consistent = stats
        .typed_allocations
        .saturating_add(stats.fallback_allocations)
        == stats.total_allocations;
    let deallocation_counters_consistent = classified_deallocations == stats.total_deallocations;
    let allocated_byte_counters_consistent = stats
        .typed_allocated_bytes
        .saturating_add(stats.fallback_allocated_bytes)
        == stats.total_allocated_bytes;
    let coverage_basis_points_valid = stats.coverage_basis_points <= 10_000;
    let live_allocations = stats
        .total_allocations
        .saturating_sub(stats.total_deallocations);
    let activity_observed = report.allocator_stats_recording_active
        && stats.total_allocations > 0
        && stats.total_deallocations > 0;
    format!(
        concat!(
            "\"allocator_stats_recording_active\":{},",
            "\"unialloc_allocator_activity_observed\":{},",
            "\"allocator_allocation_counters_consistent\":{},",
            "\"allocator_deallocation_counters_consistent\":{},",
            "\"allocator_allocated_byte_counters_consistent\":{},",
            "\"allocator_coverage_basis_points_valid\":{},",
            "\"allocator_total_allocations\":{},",
            "\"allocator_typed_allocations\":{},",
            "\"allocator_fallback_allocations\":{},",
            "\"allocator_live_allocations\":{},",
            "\"allocator_total_deallocations\":{},",
            "\"allocator_typed_deallocations\":{},",
            "\"allocator_fallback_deallocations\":{},",
            "\"allocator_total_allocated_bytes\":{},",
            "\"allocator_typed_allocated_bytes\":{},",
            "\"allocator_fallback_allocated_bytes\":{},",
            "\"allocator_coverage_basis_points\":{},",
            "\"allocator_type_stats_dropped_events\":{}"
        ),
        bool_json(report.allocator_stats_recording_active),
        bool_json(activity_observed),
        bool_json(allocation_counters_consistent),
        bool_json(deallocation_counters_consistent),
        bool_json(allocated_byte_counters_consistent),
        bool_json(coverage_basis_points_valid),
        stats.total_allocations,
        stats.typed_allocations,
        stats.fallback_allocations,
        live_allocations,
        stats.total_deallocations,
        stats.typed_deallocations,
        stats.fallback_deallocations,
        stats.total_allocated_bytes,
        stats.typed_allocated_bytes,
        stats.fallback_allocated_bytes,
        stats.coverage_basis_points,
        stats.semantic_type_stats_dropped_events
    )
}

fn success_json(report: WorkloadReport) -> String {
    format!(
        "{{\"source\":\"platform_allocator_workload\",\"passed\":true,{},{},\"threads\":{},\"iters\":{},\"max_len\":{},\"checksum\":{},\"duration_ns\":{}}}",
        platform_runtime_metadata_json(),
        allocator_stats_json(&report),
        report.cfg.threads,
        report.cfg.iters,
        report.cfg.max_len,
        report.checksum,
        report.duration_ns
    )
}

fn failure_json(cfg: Option<Config>, error: &str, duration_ns: u128) -> String {
    format!(
        "{{\"source\":\"platform_allocator_workload\",\"passed\":false,\"threads\":{},\"iters\":{},\"max_len\":{},\"checksum\":null,\"duration_ns\":{},\"error\":\"{}\"}}",
        config_number_json(cfg, |cfg| cfg.threads),
        config_number_json(cfg, |cfg| cfg.iters),
        config_number_json(cfg, |cfg| cfg.max_len),
        duration_ns,
        json_escape(error)
    )
}

fn main() {
    let cfg = match parse_config() {
        Ok(ConfigAction::Run(cfg)) => cfg,
        Ok(ConfigAction::Help) => {
            println!("{}", USAGE);
            return;
        }
        Err(err) => {
            println!("{}", failure_json(None, &err, 0));
            process::exit(2);
        }
    };

    let start = Instant::now();
    match run_workload(cfg) {
        Ok(report) => println!("{}", success_json(report)),
        Err(err) => {
            println!(
                "{}",
                failure_json(Some(cfg), &err, start.elapsed().as_nanos())
            );
            process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn aggregate_checksum_does_not_cancel_equal_workers() {
        let cfg = Config {
            threads: 2,
            iters: 32,
            max_len: 1024,
        };
        let checksum = aggregate_worker_checksums(cfg, [0x1234usize, 0x1234usize]);
        assert_ne!(checksum, 0);
    }

    #[test]
    fn aggregate_checksum_depends_on_runtime_shape() {
        let base = aggregate_worker_checksums(
            Config {
                threads: 2,
                iters: 32,
                max_len: 1024,
            },
            [1usize, 2usize],
        );
        let changed = aggregate_worker_checksums(
            Config {
                threads: 2,
                iters: 33,
                max_len: 1024,
            },
            [1usize, 2usize],
        );
        assert_ne!(base, changed);
    }

    #[test]
    fn parse_config_rejects_bad_inputs_without_panicking() {
        assert!(parse_config_from(["--unknown".to_string()]).is_err());
        assert!(parse_config_from(["--threads".to_string()]).is_err());
        assert!(parse_config_from(["--threads".to_string(), "0".to_string()]).is_err());
        assert!(parse_config_from(["--iters".to_string(), "0".to_string()]).is_err());
        assert!(parse_config_from(["--max-len".to_string(), "63".to_string()]).is_err());
        assert!(parse_config_from(["--max-len".to_string(), "not-a-number".to_string()]).is_err());
    }

    #[test]
    fn parse_config_accepts_explicit_workload_shape() {
        let parsed = parse_config_from([
            "--threads".to_string(),
            "4".to_string(),
            "--iters".to_string(),
            "2000".to_string(),
            "--max-len".to_string(),
            "2048".to_string(),
        ])
        .expect("valid workload config");
        let cfg = match parsed {
            ConfigAction::Run(cfg) => cfg,
            ConfigAction::Help => panic!("valid workload config should not be help"),
        };
        assert_eq!(cfg.threads, 4);
        assert_eq!(cfg.iters, 2000);
        assert_eq!(cfg.max_len, 2048);
    }

    #[test]
    fn success_json_reports_real_allocator_platform_metadata() {
        let json = success_json(WorkloadReport {
            cfg: Config {
                threads: 1,
                iters: 1,
                max_len: 64,
            },
            checksum: 7,
            duration_ns: 42,
            allocator_stats: SemanticStatsSnapshot {
                total_allocations: 3,
                typed_allocations: 0,
                fallback_allocations: 3,
                total_allocated_bytes: 192,
                typed_allocated_bytes: 0,
                fallback_allocated_bytes: 192,
                total_deallocations: 3,
                typed_deallocations: 0,
                fallback_deallocations: 3,
                policy_flags_seen: 0,
                last_type_id: 0,
                coverage_basis_points: 0,
                typed_cache_hits: 0,
                typed_cache_inserts: 0,
                typed_cache_bypasses: 0,
                delayed_free_enqueues: 0,
                delayed_free_flushes: 0,
                metadata_pac_auth_signs: 0,
                metadata_pac_auth_verifications: 0,
                metadata_pac_auth_failures: 0,
                metadata_pac_software_fallback_signs: 0,
                metadata_pac_software_fallback_verifications: 0,
                metadata_pac_software_fallback_failures: 0,
                semantic_type_stats_dropped_events: 0,
            },
            allocator_stats_recording_active: true,
        });
        assert!(json.contains("\"allocator\":\"UniAlloc\""));
        assert!(json.contains("\"global_allocator_active\":true"));
        assert!(json.contains("\"allocator_stats_recording_active\":true"));
        assert!(json.contains("\"unialloc_allocator_activity_observed\":true"));
        assert!(json.contains("\"allocator_allocation_counters_consistent\":true"));
        assert!(json.contains("\"allocator_deallocation_counters_consistent\":true"));
        assert!(json.contains("\"allocator_allocated_byte_counters_consistent\":true"));
        assert!(json.contains("\"allocator_coverage_basis_points_valid\":true"));
        assert!(json.contains("\"allocator_total_allocations\":3"));
        assert!(json.contains("\"allocator_live_allocations\":0"));
        assert!(json.contains("\"allocator_total_deallocations\":3"));
        assert!(json.contains("\"allocator_type_stats_dropped_events\":0"));
        assert!(json.contains("\"allocator_backend\":"));
        assert!(json.contains("\"thread_local_backend\":"));
        assert!(json.contains("\"target_os\":"));
    }

    #[test]
    fn failure_json_is_structured_and_escaped() {
        let json = failure_json(None, "bad \"arg\"\\value", 0);
        assert!(json.contains("\"passed\":false"));
        assert!(json.contains("\"threads\":null"));
        assert!(json.contains("bad \\\"arg\\\"\\\\value"));
    }
}
