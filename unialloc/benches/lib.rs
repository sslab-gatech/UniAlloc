#![cfg(not(target_os = "android"))]
#![cfg_attr(not(unialloc_btree_extract_if_range), feature(btree_drain_filter))]
#![cfg_attr(not(unialloc_has_stable_map_first_last), feature(map_first_last))]
#![feature(iter_next_chunk)]
#![feature(portable_simd)]
#![feature(slice_partition_dedup)]
#![feature(test)]

extern crate test;
#[macro_use]
extern crate alloc;

cfg_if::cfg_if! {
    if #[cfg(feature = "bench_jemalloc")] {
        use jemallocator::Jemalloc;
        #[global_allocator]
        static JEMALLOC: Jemalloc = Jemalloc;
    } else if #[cfg(feature = "bench_mimalloc")] {
        use mimalloc::MiMalloc;
        #[global_allocator]
        static MIMALLOC: MiMalloc = MiMalloc;
    } else if #[cfg(feature = "bench_tcmalloc")] {
        use tcmalloc::TCMalloc;
        #[global_allocator]
        static TCMALLOC: TCMalloc = TCMalloc;
    } else if #[cfg(feature = "bench_snmalloc")] {
        #[global_allocator]
        static ALLOC: snmalloc_rs::SnMalloc = snmalloc_rs::SnMalloc;
    } else if #[cfg(feature = "bench_scudo")] {
        use std::alloc::System;
        #[global_allocator]
        static SCUDO_SYSTEM: System = System;
    } else if #[cfg(feature = "bench_ptmalloc")] {
        use std::alloc::System;
        #[global_allocator]
        static SYSTEM: System = System;
    } else {
        // default -> ourself
        #[cfg(feature = "fixed_heap")]
        use std::alloc::System;
        #[cfg(not(feature = "fixed_heap"))]
        use unialloc::UniAlloc;
        #[cfg(feature = "fixed_heap")]
        #[global_allocator]
        static SYSTEM_FOR_FIXED_HEAP_HARNESS: System = System;
        #[cfg(not(feature = "fixed_heap"))]
        #[global_allocator]
        static OURSELF: UniAlloc = UniAlloc;
    }
}

#[cfg(all(feature = "stats", feature = "pac", target_arch = "aarch64"))]
use unialloc::alloc_api::{FLAG_METADATA_PROTECTION, FLAG_POINTER_AUTH};
#[cfg(feature = "stats")]
use unialloc::{
    alloc_api::{
        __unialloc_semantic_scope_enter, __unialloc_semantic_scope_exit,
        metadata_segregation_side_cache_snapshot, type_isolation_side_cache_snapshot,
        AllocationMetadata, FLAG_HUGEPAGE_METADATA, FLAG_METADATA_SEGREGATED,
    },
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_recording_enabled,
    semantic_type_stats_snapshot, SemanticAlloc, SemanticTypeStatsSnapshot,
};
#[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
use unialloc::{
    alloc_api::{
        hugepage_metadata_side_cache_backing_snapshot, hugepage_metadata_side_cache_snapshot,
    },
    hugepage_mmap_platform_status_snapshot, hugepage_mmap_stats_snapshot,
};
#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
use unialloc::{
    semantic_auto_compiler_metadata_enable, semantic_auto_compiler_metadata_stream_enable,
    semantic_auto_compiler_metadata_stream_thread_local_recovery_enable,
    semantic_auto_compiler_metadata_thread_local_recovery_enable, semantic_auto_metadata_disable,
    semantic_auto_metadata_enable, semantic_auto_metadata_type_id_basis, AUTO_LAYOUT_MODULE_ID,
    FLAG_TYPE_ISOLATED,
};

mod binary_heap;
mod btree;
mod linked_list;
mod slice;
mod str;
mod string;
mod vec;
mod vec_deque;

/// Returns a deterministic RNG so repeated benchmark runs use the same data.
fn bench_rng() -> rand_xorshift::XorShiftRng {
    const SEED: [u8; 16] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15];
    rand::SeedableRng::from_seed(SEED)
}

#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
const STD_BENCH_AUTO_CALLSITE: u64 = 0x5354_4442_454e_4348; // "STDBENCH"
#[cfg(feature = "stats")]
const STD_BENCH_AUTO_TYPE_SITE_ROWS: usize = 512;
#[cfg(feature = "stats")]
const STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_TYPE_ID: u64 = 0x5459_5045_4953_4f01; // "TYPEISO"
#[cfg(feature = "stats")]
const STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_CALLSITE: u64 = 0x5459_5049_534f_4348; // "TYPISOCH"
#[cfg(feature = "stats")]
const STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_TYPE_ID_BASIS: &str =
    "bench-harness-exact-semantic-type-id-type-isolation-side-cache-probe";
#[cfg(feature = "stats")]
const STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_STREAM_MODE: &str =
    "bench-harness-explicit-semantic-alloc";
#[cfg(feature = "stats")]
const STD_BENCH_HUGEPAGE_SIDE_CACHE_TYPE_ID: u64 = 0x4847_5047_5343_0001; // "HGPGSC"
#[cfg(feature = "stats")]
const STD_BENCH_HUGEPAGE_SIDE_CACHE_CALLSITE: u64 = 0x4847_5047_5343_4348; // "HGPGSCCH"
#[cfg(feature = "stats")]
const STD_BENCH_HUGEPAGE_SIDE_CACHE_TYPE_ID_BASIS: &str =
    "bench-harness-exact-semantic-type-id-hugepage-side-cache-probe";
#[cfg(feature = "stats")]
const STD_BENCH_HUGEPAGE_SIDE_CACHE_STREAM_MODE: &str = "bench-harness-explicit-semantic-alloc";
#[cfg(feature = "stats")]
const STD_BENCH_METADATA_SEGREGATION_TYPE_ID: u64 = 0x4d45_5441_5345_4701; // "METASEG"
#[cfg(feature = "stats")]
const STD_BENCH_METADATA_SEGREGATION_CALLSITE: u64 = 0x4d45_5441_5347_4348; // "METASGCH"
#[cfg(feature = "stats")]
const STD_BENCH_METADATA_SEGREGATION_TYPE_ID_BASIS: &str =
    "bench-harness-exact-semantic-type-id-metadata-segregation-side-cache-probe";
#[cfg(feature = "stats")]
const STD_BENCH_METADATA_SEGREGATION_STREAM_MODE: &str = "bench-harness-explicit-semantic-alloc";
#[cfg(feature = "stats")]
const STD_BENCH_PAC_AUTH_TYPE_ID: u64 = 0x5041_4341_5554_4801; // "PACAUTH"
#[cfg(feature = "stats")]
const STD_BENCH_PAC_AUTH_CALLSITE: u64 = 0x5041_4341_5554_4843; // "PACAUTHC"
#[cfg(feature = "stats")]
const STD_BENCH_PAC_AUTH_TYPE_ID_BASIS: &str =
    "bench-harness-exact-semantic-type-id-pac-metadata-auth-probe";
#[cfg(feature = "stats")]
const STD_BENCH_PAC_AUTH_STREAM_MODE: &str = "bench-harness-explicit-semantic-alloc";
#[cfg(feature = "stats")]
const RUSTC_DRIVER_LOWERING_MODULE_ID: u64 = 0xC002_DA00_0000_0001;
#[cfg(feature = "stats")]
const RUSTC_DRIVER_LOWERING_TYPE_ID_BASIS: &str =
    "compiler-assigned-allocation-site-object-type-id-rustc-driver-source-span-lowered";
#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
const RUSTC_DRIVER_LOWERING_STREAM_MODE: &str = "rustc-driver-source-span-lowered";
#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
const RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_TYPE_ID_BASIS: &str =
    "compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope";
#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
const RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_STREAM_MODE: &str = "rustc-driver-mir-semantic-scope";

#[cfg(feature = "stats")]
struct RustcDriverLoweredScopeGuard(Option<AllocationMetadata>);

#[cfg(feature = "stats")]
impl Drop for RustcDriverLoweredScopeGuard {
    fn drop(&mut self) {
        if let Some(previous) = self.0.take() {
            __unialloc_semantic_scope_exit(previous);
        }
    }
}

#[cfg(feature = "stats")]
#[inline(never)]
pub(crate) fn __unialloc_rustc_driver_lowered_site<R>(
    type_id: u64,
    module_id: u64,
    flags: u32,
    callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    let _guard = RustcDriverLoweredScopeGuard(Some(__unialloc_semantic_scope_enter(
        type_id, module_id, flags, callsite,
    )));
    f()
}

#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
fn parse_compiler_site_type_ids() -> Option<alloc::vec::Vec<u64>> {
    let raw = std::env::var("UNIALLOC_COMPILER_SITE_TYPE_IDS").ok()?;
    let mut ids = alloc::vec::Vec::new();
    for token in raw
        .split(|ch: char| ch == ',' || ch == ';' || ch.is_ascii_whitespace())
        .map(str::trim)
        .filter(|token| !token.is_empty())
    {
        let parsed = if let Some(hex) = token
            .strip_prefix("0x")
            .or_else(|| token.strip_prefix("0X"))
        {
            u64::from_str_radix(hex, 16).ok()
        } else {
            token.parse::<u64>().ok()
        };
        if let Some(type_id) = parsed {
            if type_id != 0 {
                ids.push(type_id);
            }
        }
        if ids.len() >= 4096 {
            break;
        }
    }
    if ids.is_empty() {
        None
    } else {
        Some(ids)
    }
}

#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
fn compiler_site_type_id_mode() -> &'static str {
    match std::env::var("UNIALLOC_COMPILER_SITE_TYPE_ID_MODE") {
        Ok(raw)
            if raw.eq_ignore_ascii_case("consuming-stream")
                || raw.eq_ignore_ascii_case("stream") =>
        {
            "consuming-stream"
        }
        _ => "cyclic-replay",
    }
}

#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
fn compiler_site_recovery_scope() -> &'static str {
    match std::env::var("UNIALLOC_COMPILER_SITE_RECOVERY_SCOPE") {
        Ok(raw)
            if raw.eq_ignore_ascii_case("thread-local")
                || raw.eq_ignore_ascii_case("thread_local")
                || raw.eq_ignore_ascii_case("tls") =>
        {
            "thread-local"
        }
        _ => "global",
    }
}

#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
fn std_bench_runtime_metadata_mode() -> &'static str {
    match std::env::var("UNIALLOC_STD_BENCH_RUNTIME_METADATA_MODE") {
        Ok(raw)
            if raw.eq_ignore_ascii_case(RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_STREAM_MODE)
                || raw.eq_ignore_ascii_case("mir-semantic-scope") =>
        {
            RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_STREAM_MODE
        }
        Ok(raw)
            if raw.eq_ignore_ascii_case(RUSTC_DRIVER_LOWERING_STREAM_MODE)
                || raw.eq_ignore_ascii_case("source-span-lowered") =>
        {
            RUSTC_DRIVER_LOWERING_STREAM_MODE
        }
        _ => "auto-metadata",
    }
}

#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
fn std_bench_mir_semantic_scope_runtime_mode() -> bool {
    std_bench_runtime_metadata_mode() == RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_STREAM_MODE
}

#[cfg(feature = "stats")]
fn std_bench_disable_type_stats() -> bool {
    std::env::var_os("UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS").is_some()
}

#[cfg(feature = "stats")]
fn std_bench_disable_aggregate_stats() -> bool {
    std::env::var_os("UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS").is_some()
}

#[cfg(feature = "stats")]
fn with_std_bench_probe_stats<R>(f: impl FnOnce() -> R) -> R {
    let aggregate_stats_were_enabled = unialloc::semantic_stats_recording_enabled();
    let type_stats_were_enabled = semantic_type_stats_recording_enabled();
    if !aggregate_stats_were_enabled {
        unialloc::semantic_stats_recording_enable();
    }
    if !type_stats_were_enabled {
        semantic_type_stats_recording_disable();
    }

    let result = f();

    if aggregate_stats_were_enabled {
        if !type_stats_were_enabled {
            semantic_type_stats_recording_disable();
        }
    } else {
        semantic_stats_recording_disable();
        if type_stats_were_enabled {
            unialloc::semantic_type_stats_recording_enable();
        }
    }
    result
}

#[cfg(feature = "stats")]
fn rustc_driver_lowered_type_id_basis() -> &'static str {
    if std_bench_mir_semantic_scope_runtime_mode() {
        RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_TYPE_ID_BASIS
    } else {
        RUSTC_DRIVER_LOWERING_TYPE_ID_BASIS
    }
}

#[cfg(feature = "stats")]
fn rustc_driver_lowered_stream_mode() -> &'static str {
    if std_bench_mir_semantic_scope_runtime_mode() {
        RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_STREAM_MODE
    } else {
        RUSTC_DRIVER_LOWERING_STREAM_MODE
    }
}

#[cfg(feature = "stats")]
fn typed_row_type_id_basis(
    row: &SemanticTypeStatsSnapshot,
    default_basis: &'static str,
) -> &'static str {
    if let Some(probe_basis) = typed_row_exact_probe_type_id_basis(row) {
        return probe_basis;
    }
    if row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID {
        rustc_driver_lowered_type_id_basis()
    } else {
        default_basis
    }
}

#[cfg(feature = "stats")]
fn typed_row_stream_mode(
    row: &SemanticTypeStatsSnapshot,
    default_mode: &'static str,
) -> &'static str {
    if let Some(probe_mode) = typed_row_exact_probe_stream_mode(row) {
        return probe_mode;
    }
    if row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID {
        rustc_driver_lowered_stream_mode()
    } else {
        default_mode
    }
}

#[cfg(feature = "stats")]
fn typed_row_exact_probe_type_id_basis(row: &SemanticTypeStatsSnapshot) -> Option<&'static str> {
    if row.type_id == STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_TYPE_ID
        && row.callsite == STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_CALLSITE
    {
        return Some(STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_TYPE_ID_BASIS);
    }
    if row.type_id == STD_BENCH_HUGEPAGE_SIDE_CACHE_TYPE_ID
        && row.callsite == STD_BENCH_HUGEPAGE_SIDE_CACHE_CALLSITE
    {
        return Some(STD_BENCH_HUGEPAGE_SIDE_CACHE_TYPE_ID_BASIS);
    }
    if row.type_id == STD_BENCH_METADATA_SEGREGATION_TYPE_ID
        && row.callsite == STD_BENCH_METADATA_SEGREGATION_CALLSITE
    {
        return Some(STD_BENCH_METADATA_SEGREGATION_TYPE_ID_BASIS);
    }
    if row.type_id == STD_BENCH_PAC_AUTH_TYPE_ID && row.callsite == STD_BENCH_PAC_AUTH_CALLSITE {
        return Some(STD_BENCH_PAC_AUTH_TYPE_ID_BASIS);
    }
    None
}

#[cfg(feature = "stats")]
fn typed_row_exact_probe_stream_mode(row: &SemanticTypeStatsSnapshot) -> Option<&'static str> {
    if row.type_id == STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_TYPE_ID
        && row.callsite == STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_CALLSITE
    {
        return Some(STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_STREAM_MODE);
    }
    if row.type_id == STD_BENCH_HUGEPAGE_SIDE_CACHE_TYPE_ID
        && row.callsite == STD_BENCH_HUGEPAGE_SIDE_CACHE_CALLSITE
    {
        return Some(STD_BENCH_HUGEPAGE_SIDE_CACHE_STREAM_MODE);
    }
    if row.type_id == STD_BENCH_METADATA_SEGREGATION_TYPE_ID
        && row.callsite == STD_BENCH_METADATA_SEGREGATION_CALLSITE
    {
        return Some(STD_BENCH_METADATA_SEGREGATION_STREAM_MODE);
    }
    if row.type_id == STD_BENCH_PAC_AUTH_TYPE_ID && row.callsite == STD_BENCH_PAC_AUTH_CALLSITE {
        return Some(STD_BENCH_PAC_AUTH_STREAM_MODE);
    }
    None
}

#[cfg(feature = "stats")]
fn typed_row_is_compiler_lowered(row: &SemanticTypeStatsSnapshot) -> bool {
    row.module_id == RUSTC_DRIVER_LOWERING_MODULE_ID
}

#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
fn std_bench_auto_policy_flags() -> u32 {
    #[cfg(any(
        feature = "metadata_segregation",
        feature = "hugepage",
        feature = "pac",
        feature = "mte",
        feature = "mpk",
        feature = "guard_pages",
        feature = "quarantine",
        feature = "force_initialize"
    ))]
    let mut flags = FLAG_TYPE_ISOLATED;
    #[cfg(not(any(
        feature = "metadata_segregation",
        feature = "hugepage",
        feature = "pac",
        feature = "mte",
        feature = "mpk",
        feature = "guard_pages",
        feature = "quarantine",
        feature = "force_initialize"
    )))]
    let flags = FLAG_TYPE_ISOLATED;
    #[cfg(feature = "metadata_segregation")]
    {
        flags |= unialloc::alloc_api::FLAG_METADATA_SEGREGATED;
    }
    #[cfg(feature = "hugepage")]
    {
        flags |= unialloc::alloc_api::FLAG_HUGEPAGE_METADATA;
    }
    #[cfg(feature = "pac")]
    {
        flags |=
            unialloc::alloc_api::FLAG_POINTER_AUTH | unialloc::alloc_api::FLAG_METADATA_PROTECTION;
    }
    #[cfg(feature = "mte")]
    {
        flags |= unialloc::alloc_api::FLAG_MEMORY_TAGGING;
    }
    #[cfg(feature = "mpk")]
    {
        flags |= unialloc::alloc_api::FLAG_METADATA_PROTECTION;
    }
    #[cfg(feature = "guard_pages")]
    {
        flags |= unialloc::alloc_api::FLAG_GUARD_PAGES;
    }
    #[cfg(feature = "quarantine")]
    {
        flags |= unialloc::alloc_api::FLAG_DELAYED_FREE;
    }
    #[cfg(feature = "force_initialize")]
    {
        flags |= unialloc::alloc_api::FLAG_FORCE_INITIALIZE;
    }
    flags
}

#[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
#[derive(Clone, Copy)]
struct HugepageMetadataSideCacheProbe {
    attempted: bool,
    allocated: bool,
    reused: bool,
    preexisting_allocated: bool,
    preexisting_mapping_backing: &'static str,
    side_cache_allocated: bool,
    side_cache_address: usize,
    side_cache_mapping_size: usize,
    mapping_backing: &'static str,
    linux_smaps_attempted: bool,
    linux_smaps_found: bool,
    linux_smaps_kernel_page_size_kb: usize,
    linux_smaps_mmu_page_size_kb: usize,
    linux_smaps_anon_huge_pages_kb: usize,
    linux_smaps_vmflags_ht: bool,
    linux_smaps_vmflags_hg: bool,
    linux_smaps_hugepage_backed: bool,
    cache_hits_delta: usize,
    cache_inserts_delta: usize,
    cache_bypasses_delta: usize,
    mmap_attempts_delta: usize,
    mmap_successes_delta: usize,
    mmap_fallbacks_delta: usize,
    mmap_aligned_fallbacks_delta: usize,
    mmap_advised_fallbacks_delta: usize,
    mmap_fallback_failures_delta: usize,
    mmap_platform_error_stage: &'static str,
    mmap_platform_error_code: isize,
    mmap_platform_error_name: &'static str,
}

#[cfg(feature = "stats")]
#[derive(Clone, Copy)]
struct TypeIsolationSideCacheProbe {
    attempted: bool,
    allocated: bool,
    reused: bool,
    side_cache_inline_occupied_before: bool,
    side_cache_inline_occupied_after: bool,
    side_cache_occupied_entries_before: usize,
    side_cache_occupied_entries_after: usize,
    side_cache_occupied_slots_before: usize,
    side_cache_occupied_slots_after: usize,
    side_cache_corrupt_slots_after: usize,
    side_cache_hot_slot_active_after: bool,
    cache_hits_delta: usize,
    cache_inserts_delta: usize,
    cache_bypasses_delta: usize,
}

#[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
#[derive(Clone, Copy)]
struct LinuxSmapsHugepageProbe {
    attempted: bool,
    found: bool,
    kernel_page_size_kb: usize,
    mmu_page_size_kb: usize,
    anon_huge_pages_kb: usize,
    vmflags_ht: bool,
    vmflags_hg: bool,
    hugepage_backed: bool,
}

#[cfg(feature = "stats")]
#[derive(Clone, Copy)]
struct SideCacheReuseProbe {
    attempted: bool,
    allocated: bool,
    reused: bool,
    /// Address returned by the first real allocator call.
    ///
    /// Feature probes use this only as an integer input to architecture probes
    /// after the object has been safely deallocated.  Keeping the address lets
    /// PAC diagnostics check the same pointer shape that the allocator cached
    /// instead of an unrelated stack representative.
    #[allow(dead_code)]
    first_allocation_addr: usize,
    cache_hits_delta: usize,
    cache_inserts_delta: usize,
    cache_bypasses_delta: usize,
    #[allow(dead_code)]
    pac_signs_delta: usize,
    #[allow(dead_code)]
    pac_verifications_delta: usize,
    #[allow(dead_code)]
    pac_failures_delta: usize,
    #[allow(dead_code)]
    pac_software_fallback_signs_delta: usize,
    #[allow(dead_code)]
    pac_software_fallback_verifications_delta: usize,
    #[allow(dead_code)]
    pac_software_fallback_failures_delta: usize,
}

#[cfg(feature = "stats")]
#[derive(Clone, Copy)]
struct MetadataSegregationSideCacheProbe {
    attempted: bool,
    allocated: bool,
    reused: bool,
    side_cache_inline_occupied_before: bool,
    side_cache_inline_occupied_after: bool,
    side_cache_occupied_entries_before: usize,
    side_cache_occupied_entries_after: usize,
    side_cache_occupied_buckets_before: usize,
    side_cache_occupied_buckets_after: usize,
    side_cache_corrupt_buckets_after: usize,
    side_cache_hot_bucket_active_after: bool,
    cache_hits_delta: usize,
    cache_inserts_delta: usize,
    cache_bypasses_delta: usize,
}

#[cfg(feature = "stats")]
#[derive(Clone, Copy)]
struct PacMetadataAuthProbe {
    attempted: bool,
    context_binding_active: bool,
    software_fallback_active: bool,
    pac_probe_uses_allocated_object: bool,
    pac_probe_key: &'static str,
    pac_probe_active_key: &'static str,
    pac_probe_key_matrix_keys: [&'static str; 4],
    pac_probe_key_matrix_available: [bool; 4],
    pac_probe_key_matrix_signed_changed: [bool; 4],
    pac_probe_key_matrix_strip_roundtrip: [bool; 4],
    pac_probe_key_matrix_correct_context_roundtrip: [bool; 4],
    pac_probe_key_matrix_wrong_context_rejected: [bool; 4],
    pac_probe_signed_changed: bool,
    pac_probe_strip_roundtrip: bool,
    pac_probe_correct_context_roundtrip: bool,
    pac_probe_wrong_context_rejected: bool,
    allocated: bool,
    reused: bool,
    cache_hits_delta: usize,
    cache_inserts_delta: usize,
    cache_bypasses_delta: usize,
    pac_signs_delta: usize,
    pac_verifications_delta: usize,
    pac_failures_delta: usize,
    pac_software_fallback_signs_delta: usize,
    pac_software_fallback_verifications_delta: usize,
    pac_software_fallback_failures_delta: usize,
}

#[cfg(feature = "stats")]
/// Exercise the real UniAlloc metadata ABI on the common feature-probe path:
/// allocate once, free into the typed side cache, then allocate again and
/// observe whether the exact object is reused.  Feature-specific wrappers add
/// their own counters (hugepage mmap deltas, PAC auth deltas) around this same
/// allocator path instead of carrying duplicate mock probe logic.
fn run_side_cache_reuse_probe(
    layout: std::alloc::Layout,
    metadata: AllocationMetadata,
) -> SideCacheReuseProbe {
    let before_stats = semantic_stats_snapshot();
    let alloc = unialloc::UniAlloc::new();

    let mut allocated = false;
    let mut reused = false;
    let mut first_allocation_addr = 0usize;
    unsafe {
        let ptr = alloc.alloc_with_metadata(layout, metadata);
        if !ptr.is_null() {
            allocated = true;
            first_allocation_addr = ptr as usize;
            alloc.dealloc_with_metadata(ptr, layout, metadata);
            let cached = alloc.alloc_with_metadata(layout, metadata);
            reused = cached == ptr && !cached.is_null();
            if !cached.is_null() {
                alloc.dealloc_with_metadata(cached, layout, metadata);
            }
        }
    }

    let after_stats = semantic_stats_snapshot();
    SideCacheReuseProbe {
        attempted: true,
        allocated,
        reused,
        first_allocation_addr,
        cache_hits_delta: after_stats
            .typed_cache_hits
            .saturating_sub(before_stats.typed_cache_hits),
        cache_inserts_delta: after_stats
            .typed_cache_inserts
            .saturating_sub(before_stats.typed_cache_inserts),
        cache_bypasses_delta: after_stats
            .typed_cache_bypasses
            .saturating_sub(before_stats.typed_cache_bypasses),
        pac_signs_delta: after_stats
            .metadata_pac_auth_signs
            .saturating_sub(before_stats.metadata_pac_auth_signs),
        pac_verifications_delta: after_stats
            .metadata_pac_auth_verifications
            .saturating_sub(before_stats.metadata_pac_auth_verifications),
        pac_failures_delta: after_stats
            .metadata_pac_auth_failures
            .saturating_sub(before_stats.metadata_pac_auth_failures),
        pac_software_fallback_signs_delta: after_stats
            .metadata_pac_software_fallback_signs
            .saturating_sub(before_stats.metadata_pac_software_fallback_signs),
        pac_software_fallback_verifications_delta: after_stats
            .metadata_pac_software_fallback_verifications
            .saturating_sub(before_stats.metadata_pac_software_fallback_verifications),
        pac_software_fallback_failures_delta: after_stats
            .metadata_pac_software_fallback_failures
            .saturating_sub(before_stats.metadata_pac_software_fallback_failures),
    }
}

#[cfg(feature = "stats")]
fn run_type_isolation_side_cache_probe() -> Option<TypeIsolationSideCacheProbe> {
    if !cfg!(feature = "type_isolation") {
        return None;
    }

    let layout = std::alloc::Layout::from_size_align(
        4 * core::mem::size_of::<usize>(),
        core::mem::align_of::<usize>(),
    )
    .ok()?;
    let metadata = AllocationMetadata::for_type(STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_TYPE_ID)
        .with_module(AUTO_LAYOUT_MODULE_ID)
        .with_callsite(STD_BENCH_TYPE_ISOLATION_SIDE_CACHE_CALLSITE)
        .with_flags(FLAG_TYPE_ISOLATED);

    let before_side_cache = type_isolation_side_cache_snapshot();
    let probe = run_side_cache_reuse_probe(layout, metadata);
    let after_side_cache = type_isolation_side_cache_snapshot();

    Some(TypeIsolationSideCacheProbe {
        attempted: probe.attempted,
        allocated: probe.allocated,
        reused: probe.reused,
        side_cache_inline_occupied_before: before_side_cache.inline_occupied,
        side_cache_inline_occupied_after: after_side_cache.inline_occupied,
        side_cache_occupied_entries_before: before_side_cache.occupied_entries,
        side_cache_occupied_entries_after: after_side_cache.occupied_entries,
        side_cache_occupied_slots_before: before_side_cache.occupied_slots,
        side_cache_occupied_slots_after: after_side_cache.occupied_slots,
        side_cache_corrupt_slots_after: after_side_cache.corrupt_slots,
        side_cache_hot_slot_active_after: after_side_cache.hot_slot_active,
        cache_hits_delta: probe.cache_hits_delta,
        cache_inserts_delta: probe.cache_inserts_delta,
        cache_bypasses_delta: probe.cache_bypasses_delta,
    })
}

#[cfg(all(feature = "stats", not(feature = "fixed_heap"), target_os = "linux"))]
fn parse_linux_smaps_range(line: &str) -> Option<(usize, usize)> {
    let range = line.split_whitespace().next()?;
    let mut parts = range.splitn(2, '-');
    let start = usize::from_str_radix(parts.next()?, 16).ok()?;
    let end = usize::from_str_radix(parts.next()?, 16).ok()?;
    if start < end {
        Some((start, end))
    } else {
        None
    }
}

#[cfg(all(feature = "stats", not(feature = "fixed_heap"), target_os = "linux"))]
fn parse_linux_smaps_kb(line: &str, key: &str) -> Option<usize> {
    line.strip_prefix(key)?
        .split_whitespace()
        .next()
        .and_then(|value| value.parse::<usize>().ok())
}

#[cfg(all(feature = "stats", not(feature = "fixed_heap"), target_os = "linux"))]
fn linux_smaps_hugepage_probe(address: usize) -> LinuxSmapsHugepageProbe {
    let mut probe = LinuxSmapsHugepageProbe {
        attempted: address != 0,
        found: false,
        kernel_page_size_kb: 0,
        mmu_page_size_kb: 0,
        anon_huge_pages_kb: 0,
        vmflags_ht: false,
        vmflags_hg: false,
        hugepage_backed: false,
    };
    if address == 0 {
        return probe;
    }

    let smaps = match std::fs::read_to_string("/proc/self/smaps") {
        Ok(smaps) => smaps,
        Err(_) => return probe,
    };
    let mut in_mapping = false;
    for line in smaps.lines() {
        if let Some((start, end)) = parse_linux_smaps_range(line) {
            if in_mapping {
                break;
            }
            in_mapping = start <= address && address < end;
            if in_mapping {
                probe.found = true;
            }
            continue;
        }
        if !in_mapping {
            continue;
        }
        if let Some(value) = parse_linux_smaps_kb(line, "KernelPageSize:") {
            probe.kernel_page_size_kb = value;
        } else if let Some(value) = parse_linux_smaps_kb(line, "MMUPageSize:") {
            probe.mmu_page_size_kb = value;
        } else if let Some(value) = parse_linux_smaps_kb(line, "AnonHugePages:") {
            probe.anon_huge_pages_kb = value;
        } else if let Some(flags) = line.strip_prefix("VmFlags:") {
            for flag in flags.split_whitespace() {
                probe.vmflags_ht |= flag == "ht";
                probe.vmflags_hg |= flag == "hg";
            }
        }
    }
    probe.hugepage_backed = probe.kernel_page_size_kb >= 2048
        || probe.mmu_page_size_kb >= 2048
        || probe.anon_huge_pages_kb > 0
        || probe.vmflags_ht;
    probe
}

#[cfg(all(
    feature = "stats",
    not(feature = "fixed_heap"),
    not(target_os = "linux")
))]
fn linux_smaps_hugepage_probe(_address: usize) -> LinuxSmapsHugepageProbe {
    LinuxSmapsHugepageProbe {
        attempted: false,
        found: false,
        kernel_page_size_kb: 0,
        mmu_page_size_kb: 0,
        anon_huge_pages_kb: 0,
        vmflags_ht: false,
        vmflags_hg: false,
        hugepage_backed: false,
    }
}

#[cfg(all(feature = "stats", not(feature = "fixed_heap")))]
fn run_hugepage_metadata_side_cache_probe() -> Option<HugepageMetadataSideCacheProbe> {
    if !cfg!(feature = "hugepage") {
        return None;
    }

    let before_mmap = hugepage_mmap_stats_snapshot();
    let before_stats = semantic_stats_snapshot();
    let preexisting_mapping_backing = hugepage_metadata_side_cache_backing_snapshot()
        .map(|backing| backing.as_str())
        .unwrap_or("unallocated");
    let layout = std::alloc::Layout::from_size_align(
        4 * core::mem::size_of::<usize>(),
        core::mem::align_of::<usize>(),
    )
    .ok()?;
    let metadata = AllocationMetadata::for_type(STD_BENCH_HUGEPAGE_SIDE_CACHE_TYPE_ID)
        .with_module(AUTO_LAYOUT_MODULE_ID)
        .with_callsite(STD_BENCH_HUGEPAGE_SIDE_CACHE_CALLSITE)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_HUGEPAGE_METADATA);

    let alloc = unialloc::UniAlloc::new();
    let mut allocated = false;
    let mut reused = false;
    let first: *mut u8;
    let second: *mut u8;
    unsafe {
        first = alloc.alloc_with_metadata(layout, metadata);
        second = alloc.alloc_with_metadata(layout, metadata);
        if !first.is_null() && !second.is_null() {
            allocated = true;
            // The first hugepage-metadata free is intentionally kept in the
            // inline TLS entry; the second free materializes the mmap_huge
            // bucket table and moves the inline entry into that table.  This
            // keeps the probe claim-grade for the hugepage path without forcing
            // every single-object reuse to reserve a hugepage-sized mapping.
            alloc.dealloc_with_metadata(first, layout, metadata);
            alloc.dealloc_with_metadata(second, layout, metadata);
        } else {
            if !first.is_null() {
                std::alloc::GlobalAlloc::dealloc(&alloc, first, layout);
            }
            if !second.is_null() {
                std::alloc::GlobalAlloc::dealloc(&alloc, second, layout);
            }
        }
    }
    let after_mmap = hugepage_mmap_stats_snapshot();
    let platform_status = hugepage_mmap_platform_status_snapshot();
    let side_cache_snapshot = hugepage_metadata_side_cache_snapshot();
    let side_cache_address = side_cache_snapshot
        .map(|snapshot| snapshot.address)
        .unwrap_or(0);
    let side_cache_mapping_size = side_cache_snapshot
        .map(|snapshot| snapshot.mapping_size)
        .unwrap_or(0);
    let side_cache_backing = side_cache_snapshot
        .map(|snapshot| snapshot.backing.as_str())
        .unwrap_or("unallocated");
    let linux_smaps = linux_smaps_hugepage_probe(side_cache_address);
    let after_stats = unsafe {
        if allocated {
            let reused_one = alloc.alloc_with_metadata(layout, metadata);
            let reused_two = alloc.alloc_with_metadata(layout, metadata);
            reused = !reused_one.is_null()
                && !reused_two.is_null()
                && ((reused_one == first && reused_two == second)
                    || (reused_one == second && reused_two == first));
            let after_stats = semantic_stats_snapshot();
            if !reused_one.is_null() {
                std::alloc::GlobalAlloc::dealloc(&alloc, reused_one, layout);
            }
            if !reused_two.is_null() {
                std::alloc::GlobalAlloc::dealloc(&alloc, reused_two, layout);
            }
            after_stats
        } else {
            semantic_stats_snapshot()
        }
    };
    Some(HugepageMetadataSideCacheProbe {
        attempted: true,
        allocated,
        reused,
        preexisting_allocated: preexisting_mapping_backing != "unallocated",
        preexisting_mapping_backing,
        side_cache_allocated: side_cache_snapshot.is_some(),
        side_cache_address,
        side_cache_mapping_size,
        mapping_backing: side_cache_backing,
        linux_smaps_attempted: linux_smaps.attempted,
        linux_smaps_found: linux_smaps.found,
        linux_smaps_kernel_page_size_kb: linux_smaps.kernel_page_size_kb,
        linux_smaps_mmu_page_size_kb: linux_smaps.mmu_page_size_kb,
        linux_smaps_anon_huge_pages_kb: linux_smaps.anon_huge_pages_kb,
        linux_smaps_vmflags_ht: linux_smaps.vmflags_ht,
        linux_smaps_vmflags_hg: linux_smaps.vmflags_hg,
        linux_smaps_hugepage_backed: linux_smaps.hugepage_backed,
        cache_hits_delta: after_stats
            .typed_cache_hits
            .saturating_sub(before_stats.typed_cache_hits),
        cache_inserts_delta: after_stats
            .typed_cache_inserts
            .saturating_sub(before_stats.typed_cache_inserts),
        cache_bypasses_delta: after_stats
            .typed_cache_bypasses
            .saturating_sub(before_stats.typed_cache_bypasses),
        mmap_attempts_delta: after_mmap.attempts.saturating_sub(before_mmap.attempts),
        mmap_successes_delta: after_mmap.successes.saturating_sub(before_mmap.successes),
        mmap_fallbacks_delta: after_mmap.fallbacks.saturating_sub(before_mmap.fallbacks),
        mmap_aligned_fallbacks_delta: after_mmap
            .aligned_fallbacks
            .saturating_sub(before_mmap.aligned_fallbacks),
        mmap_advised_fallbacks_delta: after_mmap
            .advised_fallbacks
            .saturating_sub(before_mmap.advised_fallbacks),
        mmap_fallback_failures_delta: after_mmap
            .fallback_failures
            .saturating_sub(before_mmap.fallback_failures),
        mmap_platform_error_stage: platform_status.last_error_stage.as_str(),
        mmap_platform_error_code: platform_status.last_error_code,
        mmap_platform_error_name: platform_status.last_error_code_name(),
    })
}

#[cfg(feature = "stats")]
fn run_metadata_segregation_side_cache_probe() -> Option<MetadataSegregationSideCacheProbe> {
    if !cfg!(feature = "metadata_segregation") {
        return None;
    }

    let layout = std::alloc::Layout::from_size_align(
        2 * core::mem::size_of::<usize>(),
        core::mem::align_of::<usize>(),
    )
    .ok()?;
    let metadata = AllocationMetadata::for_type(STD_BENCH_METADATA_SEGREGATION_TYPE_ID)
        .with_module(AUTO_LAYOUT_MODULE_ID)
        .with_callsite(STD_BENCH_METADATA_SEGREGATION_CALLSITE)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_METADATA_SEGREGATED);

    let before_side_cache = metadata_segregation_side_cache_snapshot();
    let probe = run_side_cache_reuse_probe(layout, metadata);
    let after_side_cache = metadata_segregation_side_cache_snapshot();

    Some(MetadataSegregationSideCacheProbe {
        attempted: probe.attempted,
        allocated: probe.allocated,
        reused: probe.reused,
        side_cache_inline_occupied_before: before_side_cache.inline_occupied,
        side_cache_inline_occupied_after: after_side_cache.inline_occupied,
        side_cache_occupied_entries_before: before_side_cache.occupied_entries,
        side_cache_occupied_entries_after: after_side_cache.occupied_entries,
        side_cache_occupied_buckets_before: before_side_cache.occupied_buckets,
        side_cache_occupied_buckets_after: after_side_cache.occupied_buckets,
        side_cache_corrupt_buckets_after: after_side_cache.corrupt_buckets,
        side_cache_hot_bucket_active_after: after_side_cache.hot_bucket_active,
        cache_hits_delta: probe.cache_hits_delta,
        cache_inserts_delta: probe.cache_inserts_delta,
        cache_bypasses_delta: probe.cache_bypasses_delta,
    })
}

#[cfg(feature = "stats")]
fn run_pac_metadata_auth_probe() -> Option<PacMetadataAuthProbe> {
    #[cfg(not(all(feature = "pac", target_arch = "aarch64")))]
    {
        None
    }
    #[cfg(all(feature = "pac", target_arch = "aarch64"))]
    {
        let layout = std::alloc::Layout::from_size_align(
            3 * core::mem::size_of::<usize>(),
            core::mem::align_of::<usize>(),
        )
        .ok()?;
        let metadata = AllocationMetadata::for_type(STD_BENCH_PAC_AUTH_TYPE_ID)
            .with_module(AUTO_LAYOUT_MODULE_ID)
            .with_callsite(STD_BENCH_PAC_AUTH_CALLSITE)
            .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH | FLAG_METADATA_PROTECTION);

        let probe = run_side_cache_reuse_probe(layout, metadata);
        let pac_probe_uses_allocated_object = probe.first_allocation_addr != 0;
        let pac_probe_addr = if pac_probe_uses_allocated_object {
            probe.first_allocation_addr
        } else {
            // Fail-safe diagnostic fallback: if allocation failed, still report
            // target PAC behavior without pretending it covers allocator reuse.
            &probe as *const SideCacheReuseProbe as usize
        };
        let pac_probe = unialloc::pac::best_context_binding_probe_for(pac_probe_addr);
        let pac_probe_matrix = unialloc::pac::context_binding_probe_matrix_for(pac_probe_addr);
        let context_binding_active = unialloc::pac::context_binding_available();
        let active_key = unialloc::pac::active_context_binding_key_name();

        Some(PacMetadataAuthProbe {
            attempted: probe.attempted,
            context_binding_active,
            software_fallback_active: !context_binding_active,
            pac_probe_uses_allocated_object,
            pac_probe_key: pac_probe.key.as_str(),
            pac_probe_active_key: active_key,
            pac_probe_key_matrix_keys: [
                pac_probe_matrix[0].key.as_str(),
                pac_probe_matrix[1].key.as_str(),
                pac_probe_matrix[2].key.as_str(),
                pac_probe_matrix[3].key.as_str(),
            ],
            pac_probe_key_matrix_available: [
                pac_probe_matrix[0].available,
                pac_probe_matrix[1].available,
                pac_probe_matrix[2].available,
                pac_probe_matrix[3].available,
            ],
            pac_probe_key_matrix_signed_changed: [
                pac_probe_matrix[0].signed_changed,
                pac_probe_matrix[1].signed_changed,
                pac_probe_matrix[2].signed_changed,
                pac_probe_matrix[3].signed_changed,
            ],
            pac_probe_key_matrix_strip_roundtrip: [
                pac_probe_matrix[0].strip_roundtrip,
                pac_probe_matrix[1].strip_roundtrip,
                pac_probe_matrix[2].strip_roundtrip,
                pac_probe_matrix[3].strip_roundtrip,
            ],
            pac_probe_key_matrix_correct_context_roundtrip: [
                pac_probe_matrix[0].correct_context_roundtrip,
                pac_probe_matrix[1].correct_context_roundtrip,
                pac_probe_matrix[2].correct_context_roundtrip,
                pac_probe_matrix[3].correct_context_roundtrip,
            ],
            pac_probe_key_matrix_wrong_context_rejected: [
                pac_probe_matrix[0].wrong_context_rejected,
                pac_probe_matrix[1].wrong_context_rejected,
                pac_probe_matrix[2].wrong_context_rejected,
                pac_probe_matrix[3].wrong_context_rejected,
            ],
            pac_probe_signed_changed: pac_probe.signed_changed,
            pac_probe_strip_roundtrip: pac_probe.strip_roundtrip,
            pac_probe_correct_context_roundtrip: pac_probe.correct_context_roundtrip,
            pac_probe_wrong_context_rejected: pac_probe.wrong_context_rejected,
            allocated: probe.allocated,
            reused: probe.reused,
            cache_hits_delta: probe.cache_hits_delta,
            cache_inserts_delta: probe.cache_inserts_delta,
            cache_bypasses_delta: probe.cache_bypasses_delta,
            pac_signs_delta: probe.pac_signs_delta,
            pac_verifications_delta: probe.pac_verifications_delta,
            pac_failures_delta: probe.pac_failures_delta,
            pac_software_fallback_signs_delta: probe.pac_software_fallback_signs_delta,
            pac_software_fallback_verifications_delta: probe
                .pac_software_fallback_verifications_delta,
            pac_software_fallback_failures_delta: probe.pac_software_fallback_failures_delta,
        })
    }
}

#[cfg(any(
    feature = "stats",
    feature = "type_isolation",
    feature = "metadata_segregation",
    feature = "hugepage",
    feature = "pac",
    feature = "mte",
    feature = "mpk",
    feature = "guard_pages",
    feature = "quarantine",
    feature = "force_initialize"
))]
#[bench]
fn aaa_semantic_auto_metadata_enable(b: &mut test::Bencher) {
    #[cfg(feature = "stats")]
    {
        semantic_stats_reset();
        if std_bench_disable_type_stats() {
            semantic_type_stats_recording_disable();
        }
        if std_bench_disable_aggregate_stats() {
            semantic_stats_recording_disable();
        }
    }
    if std::env::var_os("UNIALLOC_STD_BENCH_DISABLE_AUTO_METADATA").is_some()
        || std_bench_mir_semantic_scope_runtime_mode()
    {
        semantic_auto_metadata_disable();
        b.iter(|| test::black_box(()));
        return;
    }
    let policy_flags = std_bench_auto_policy_flags();
    if let Some(type_ids) = parse_compiler_site_type_ids() {
        // The runtime ABI deliberately borrows this compiler-site id stream
        // until auto metadata is disabled.  Leak the small benchmark-owned
        // slice for the process lifetime instead of keeping a shared
        // `static mut Option<Vec<_>>`, which avoids creating references to a
        // mutable static while preserving the documented pointer-lifetime
        // contract.
        let stored: &'static [u64] = Box::leak(type_ids.into_boxed_slice());
        unsafe {
            let stream_mode = compiler_site_type_id_mode() == "consuming-stream";
            let thread_local_recovery = compiler_site_recovery_scope() == "thread-local";
            let installed = match (stream_mode, thread_local_recovery) {
                (true, true) => {
                    semantic_auto_compiler_metadata_stream_thread_local_recovery_enable(
                        AUTO_LAYOUT_MODULE_ID,
                        policy_flags,
                        STD_BENCH_AUTO_CALLSITE,
                        stored.as_ptr(),
                        stored.len(),
                    )
                }
                (true, false) => semantic_auto_compiler_metadata_stream_enable(
                    AUTO_LAYOUT_MODULE_ID,
                    policy_flags,
                    STD_BENCH_AUTO_CALLSITE,
                    stored.as_ptr(),
                    stored.len(),
                ),
                (false, true) => semantic_auto_compiler_metadata_thread_local_recovery_enable(
                    AUTO_LAYOUT_MODULE_ID,
                    policy_flags,
                    STD_BENCH_AUTO_CALLSITE,
                    stored.as_ptr(),
                    stored.len(),
                ),
                (false, false) => semantic_auto_compiler_metadata_enable(
                    AUTO_LAYOUT_MODULE_ID,
                    policy_flags,
                    STD_BENCH_AUTO_CALLSITE,
                    stored.as_ptr(),
                    stored.len(),
                ),
            };
            if !installed {
                semantic_auto_metadata_enable(
                    AUTO_LAYOUT_MODULE_ID,
                    policy_flags,
                    STD_BENCH_AUTO_CALLSITE,
                );
            }
        }
    } else {
        semantic_auto_metadata_enable(AUTO_LAYOUT_MODULE_ID, policy_flags, STD_BENCH_AUTO_CALLSITE);
    }

    // Ensure filtered smoke runs still produce at least one ordinary
    // `GlobalAlloc` event under auto metadata before the final report bench.
    let mut marker = alloc::vec::Vec::with_capacity(16);
    marker.push(1u8);
    test::black_box(marker.as_ptr());
    drop(marker);

    b.iter(|| test::black_box(()));
}

#[cfg(feature = "stats")]
#[bench]
fn zzz_semantic_auto_metadata_report(b: &mut test::Bencher) {
    let snapshot = semantic_stats_snapshot();
    let type_stats_recording = semantic_type_stats_recording_enabled();
    let mut type_rows = [SemanticTypeStatsSnapshot::empty(); STD_BENCH_AUTO_TYPE_SITE_ROWS];
    let type_row_count = semantic_type_stats_snapshot(&mut type_rows);
    let auto_type_id_basis = semantic_auto_metadata_type_id_basis();
    let type_id_basis = if std_bench_mir_semantic_scope_runtime_mode() {
        RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_TYPE_ID_BASIS
    } else {
        auto_type_id_basis
    };
    let compiler_site_replay =
        !std_bench_mir_semantic_scope_runtime_mode() && type_id_basis.contains("compiler-assigned");
    let compiler_site_id_stream_mode = if std_bench_mir_semantic_scope_runtime_mode() {
        RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_STREAM_MODE
    } else if type_id_basis.contains("consuming-stream") {
        "consuming-stream"
    } else if type_id_basis.contains("cyclic-replay") {
        "cyclic-replay"
    } else {
        "none"
    };

    #[cfg(not(feature = "fixed_heap"))]
    let (
        type_isolation_side_cache_probe,
        hugepage_side_cache_probe,
        metadata_segregation_side_cache_probe,
        pac_metadata_auth_probe,
    ) = with_std_bench_probe_stats(|| {
        (
            run_type_isolation_side_cache_probe(),
            run_hugepage_metadata_side_cache_probe(),
            run_metadata_segregation_side_cache_probe(),
            run_pac_metadata_auth_probe(),
        )
    });
    #[cfg(feature = "fixed_heap")]
    let (
        type_isolation_side_cache_probe,
        metadata_segregation_side_cache_probe,
        pac_metadata_auth_probe,
    ) = with_std_bench_probe_stats(|| {
        (
            run_type_isolation_side_cache_probe(),
            run_metadata_segregation_side_cache_probe(),
            run_pac_metadata_auth_probe(),
        )
    });

    semantic_auto_metadata_disable();

    let mut emitted_type_site_rows = 0usize;
    if type_row_count <= type_rows.len() {
        for row in type_rows.iter().take(type_row_count) {
            if row.allocations == 0 && row.deallocations == 0 {
                continue;
            }
            let row_type_id_basis = typed_row_type_id_basis(row, type_id_basis);
            let row_stream_mode = typed_row_stream_mode(row, compiler_site_id_stream_mode);
            let row_compiler_lowered = typed_row_is_compiler_lowered(row);
            emitted_type_site_rows += 1;
            println!(
                concat!(
                    "{{",
                    "\"source\":\"std_bench_auto_metadata\",",
                    "\"type_id_basis\":\"{}\",",
                    "\"compiler_site_replay\":{},",
                    "\"compiler_site_id_stream_mode\":\"{}\",",
                    "\"compiler_site_lowered\":{},",
                    "\"type_site_stats_recording\":{},",
                    "\"event\":\"typed_allocation_site\",",
                    "\"typed\":true,",
                    "\"type_id\":{},",
                    "\"module_id\":{},",
                    "\"callsite\":{},",
                    "\"count\":{},",
                    "\"bytes\":{},",
                    "\"deallocations\":{},",
                    "\"cache_hits\":{},",
                    "\"cache_inserts\":{},",
                    "\"cache_bypasses\":{},",
                    "\"policy_flags_seen\":{},",
                    "\"site_count\":{}",
                    "}}"
                ),
                row_type_id_basis,
                compiler_site_replay,
                row_stream_mode,
                row_compiler_lowered,
                type_stats_recording,
                row.type_id,
                row.module_id,
                row.callsite,
                row.allocations,
                row.allocated_bytes,
                row.deallocations,
                row.cache_hits,
                row.cache_inserts,
                row.cache_bypasses,
                row.policy_flags_seen,
                type_row_count
            );
        }
    }

    if emitted_type_site_rows == 0 {
        println!(
            concat!(
                "{{",
                "\"source\":\"std_bench_auto_metadata\",",
                "\"type_id_basis\":\"{}\",",
                "\"compiler_site_replay\":{},",
                "\"compiler_site_id_stream_mode\":\"{}\",",
                "\"event\":\"typed_allocations\",",
                "\"type_site_stats_recording\":{},",
                "\"typed\":true,",
                "\"count\":{},",
                "\"bytes\":{},",
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"cache_bypasses\":{},",
                "\"delayed_free_enqueues\":{},",
                "\"delayed_free_flushes\":{},",
                "\"policy_flags_seen\":{},",
                "\"metadata_pac_auth_signs\":{},",
                "\"metadata_pac_auth_verifications\":{},",
                "\"metadata_pac_auth_failures\":{},",
                "\"metadata_pac_software_fallback_signs\":{},",
                "\"metadata_pac_software_fallback_verifications\":{},",
                "\"metadata_pac_software_fallback_failures\":{},",
                "\"type_site_rows_available\":{},",
                "\"type_site_row_capacity\":{}",
                "}}"
            ),
            type_id_basis,
            compiler_site_replay,
            compiler_site_id_stream_mode,
            type_stats_recording,
            snapshot.typed_allocations,
            snapshot.typed_allocated_bytes,
            snapshot.typed_deallocations,
            snapshot.typed_cache_hits,
            snapshot.typed_cache_inserts,
            snapshot.typed_cache_bypasses,
            snapshot.delayed_free_enqueues,
            snapshot.delayed_free_flushes,
            snapshot.policy_flags_seen,
            snapshot.metadata_pac_auth_signs,
            snapshot.metadata_pac_auth_verifications,
            snapshot.metadata_pac_auth_failures,
            snapshot.metadata_pac_software_fallback_signs,
            snapshot.metadata_pac_software_fallback_verifications,
            snapshot.metadata_pac_software_fallback_failures,
            type_row_count,
            type_rows.len()
        );
    }
    println!(
        concat!(
            "{{",
            "\"source\":\"std_bench_auto_metadata\",",
            "\"type_id_basis\":\"{}\",",
            "\"compiler_site_replay\":{},",
            "\"compiler_site_id_stream_mode\":\"{}\",",
            "\"event\":\"fallback_allocations\",",
            "\"type_site_stats_recording\":{},",
            "\"typed\":false,",
            "\"count\":{},",
            "\"bytes\":{},",
            "\"deallocations\":{}",
            "}}"
        ),
        type_id_basis,
        compiler_site_replay,
        compiler_site_id_stream_mode,
        type_stats_recording,
        snapshot.fallback_allocations,
        snapshot.fallback_allocated_bytes,
        snapshot.fallback_deallocations
    );
    if let Some(probe) = type_isolation_side_cache_probe {
        println!(
            concat!(
                "{{",
                "\"source\":\"std_bench_auto_metadata\",",
                "\"event\":\"type_isolation_side_cache_probe\",",
                "\"feature_type_isolation\":{},",
                "\"attempted\":{},",
                "\"allocated\":{},",
                "\"reused\":{},",
                "\"side_cache_inline_occupied_before\":{},",
                "\"side_cache_inline_occupied_after\":{},",
                "\"side_cache_occupied_entries_before\":{},",
                "\"side_cache_occupied_entries_after\":{},",
                "\"side_cache_occupied_slots_before\":{},",
                "\"side_cache_occupied_slots_after\":{},",
                "\"side_cache_corrupt_slots_after\":{},",
                "\"side_cache_hot_slot_active_after\":{},",
                "\"cache_hits_delta\":{},",
                "\"cache_inserts_delta\":{},",
                "\"cache_bypasses_delta\":{}",
                "}}"
            ),
            cfg!(feature = "type_isolation"),
            probe.attempted,
            probe.allocated,
            probe.reused,
            probe.side_cache_inline_occupied_before,
            probe.side_cache_inline_occupied_after,
            probe.side_cache_occupied_entries_before,
            probe.side_cache_occupied_entries_after,
            probe.side_cache_occupied_slots_before,
            probe.side_cache_occupied_slots_after,
            probe.side_cache_corrupt_slots_after,
            probe.side_cache_hot_slot_active_after,
            probe.cache_hits_delta,
            probe.cache_inserts_delta,
            probe.cache_bypasses_delta
        );
    }
    #[cfg(not(feature = "fixed_heap"))]
    {
        let hugepage = hugepage_mmap_stats_snapshot();
        let hugepage_platform = hugepage_mmap_platform_status_snapshot();
        println!(
            concat!(
                "{{",
                "\"source\":\"std_bench_auto_metadata\",",
                "\"event\":\"hugepage_mmap_stats\",",
                "\"feature_hugepage\":{},",
                "\"attempts\":{},",
                "\"successes\":{},",
                "\"fallbacks\":{},",
                "\"aligned_fallbacks\":{},",
                "\"advised_fallbacks\":{},",
                "\"fallback_failures\":{},",
                "\"platform_error_stage\":\"{}\",",
                "\"platform_error_code\":{},",
                "\"platform_error_name\":\"{}\"",
                "}}"
            ),
            cfg!(feature = "hugepage"),
            hugepage.attempts,
            hugepage.successes,
            hugepage.fallbacks,
            hugepage.aligned_fallbacks,
            hugepage.advised_fallbacks,
            hugepage.fallback_failures,
            hugepage_platform.last_error_stage.as_str(),
            hugepage_platform.last_error_code,
            hugepage_platform.last_error_code_name()
        );
        if let Some(probe) = hugepage_side_cache_probe {
            println!(
                concat!(
                    "{{",
                    "\"source\":\"std_bench_auto_metadata\",",
                    "\"event\":\"hugepage_metadata_side_cache_probe\",",
                    "\"feature_hugepage\":{},",
                    "\"attempted\":{},",
                    "\"allocated\":{},",
                    "\"reused\":{},",
                    "\"preexisting_allocated\":{},",
                    "\"preexisting_mapping_backing\":\"{}\",",
                    "\"side_cache_allocated\":{},",
                    "\"side_cache_address\":{},",
                    "\"side_cache_mapping_size\":{},",
                    "\"mapping_backing\":\"{}\",",
                    "\"linux_smaps_attempted\":{},",
                    "\"linux_smaps_found\":{},",
                    "\"linux_smaps_kernel_page_size_kb\":{},",
                    "\"linux_smaps_mmu_page_size_kb\":{},",
                    "\"linux_smaps_anon_huge_pages_kb\":{},",
                    "\"linux_smaps_vmflags_ht\":{},",
                    "\"linux_smaps_vmflags_hg\":{},",
                    "\"linux_smaps_hugepage_backed\":{},",
                    "\"cache_hits_delta\":{},",
                    "\"cache_inserts_delta\":{},",
                    "\"cache_bypasses_delta\":{},",
                    "\"mmap_attempts_delta\":{},",
                    "\"mmap_successes_delta\":{},",
                    "\"mmap_fallbacks_delta\":{},",
                    "\"mmap_aligned_fallbacks_delta\":{},",
                    "\"mmap_advised_fallbacks_delta\":{},",
                    "\"mmap_fallback_failures_delta\":{},",
                    "\"mmap_platform_error_stage\":\"{}\",",
                    "\"mmap_platform_error_code\":{},",
                    "\"mmap_platform_error_name\":\"{}\"",
                    "}}"
                ),
                cfg!(feature = "hugepage"),
                probe.attempted,
                probe.allocated,
                probe.reused,
                probe.preexisting_allocated,
                probe.preexisting_mapping_backing,
                probe.side_cache_allocated,
                probe.side_cache_address,
                probe.side_cache_mapping_size,
                probe.mapping_backing,
                probe.linux_smaps_attempted,
                probe.linux_smaps_found,
                probe.linux_smaps_kernel_page_size_kb,
                probe.linux_smaps_mmu_page_size_kb,
                probe.linux_smaps_anon_huge_pages_kb,
                probe.linux_smaps_vmflags_ht,
                probe.linux_smaps_vmflags_hg,
                probe.linux_smaps_hugepage_backed,
                probe.cache_hits_delta,
                probe.cache_inserts_delta,
                probe.cache_bypasses_delta,
                probe.mmap_attempts_delta,
                probe.mmap_successes_delta,
                probe.mmap_fallbacks_delta,
                probe.mmap_aligned_fallbacks_delta,
                probe.mmap_advised_fallbacks_delta,
                probe.mmap_fallback_failures_delta,
                probe.mmap_platform_error_stage,
                probe.mmap_platform_error_code,
                probe.mmap_platform_error_name
            );
        }
    }
    if let Some(probe) = metadata_segregation_side_cache_probe {
        println!(
            concat!(
                "{{",
                "\"source\":\"std_bench_auto_metadata\",",
                "\"event\":\"metadata_segregation_side_cache_probe\",",
                "\"feature_metadata_segregation\":{},",
                "\"attempted\":{},",
                "\"allocated\":{},",
                "\"reused\":{},",
                "\"side_cache_inline_occupied_before\":{},",
                "\"side_cache_inline_occupied_after\":{},",
                "\"side_cache_occupied_entries_before\":{},",
                "\"side_cache_occupied_entries_after\":{},",
                "\"side_cache_occupied_buckets_before\":{},",
                "\"side_cache_occupied_buckets_after\":{},",
                "\"side_cache_corrupt_buckets_after\":{},",
                "\"side_cache_hot_bucket_active_after\":{},",
                "\"cache_hits_delta\":{},",
                "\"cache_inserts_delta\":{},",
                "\"cache_bypasses_delta\":{}",
                "}}"
            ),
            cfg!(feature = "metadata_segregation"),
            probe.attempted,
            probe.allocated,
            probe.reused,
            probe.side_cache_inline_occupied_before,
            probe.side_cache_inline_occupied_after,
            probe.side_cache_occupied_entries_before,
            probe.side_cache_occupied_entries_after,
            probe.side_cache_occupied_buckets_before,
            probe.side_cache_occupied_buckets_after,
            probe.side_cache_corrupt_buckets_after,
            probe.side_cache_hot_bucket_active_after,
            probe.cache_hits_delta,
            probe.cache_inserts_delta,
            probe.cache_bypasses_delta
        );
    }
    if let Some(probe) = pac_metadata_auth_probe {
        println!(
            concat!(
                "{{",
                "\"source\":\"std_bench_auto_metadata\",",
                "\"event\":\"pac_metadata_auth_probe\",",
                "\"feature_pac\":{},",
                "\"target_arch_aarch64\":{},",
                "\"attempted\":{},",
                "\"pac_probe_uses_allocated_object\":{},",
                "\"context_binding_active\":{},",
                "\"software_fallback_active\":{},",
                "\"pac_probe_key\":\"{}\",",
                "\"pac_probe_active_key\":\"{}\",",
                "\"pac_probe_key_matrix\":[",
                "{{\"key\":\"{}\",\"available\":{},\"signed_changed\":{},\"strip_roundtrip\":{},\"correct_context_roundtrip\":{},\"wrong_context_rejected\":{}}},",
                "{{\"key\":\"{}\",\"available\":{},\"signed_changed\":{},\"strip_roundtrip\":{},\"correct_context_roundtrip\":{},\"wrong_context_rejected\":{}}},",
                "{{\"key\":\"{}\",\"available\":{},\"signed_changed\":{},\"strip_roundtrip\":{},\"correct_context_roundtrip\":{},\"wrong_context_rejected\":{}}},",
                "{{\"key\":\"{}\",\"available\":{},\"signed_changed\":{},\"strip_roundtrip\":{},\"correct_context_roundtrip\":{},\"wrong_context_rejected\":{}}}",
                "],",
                "\"pac_probe_signed_changed\":{},",
                "\"pac_probe_strip_roundtrip\":{},",
                "\"pac_probe_correct_context_roundtrip\":{},",
                "\"pac_probe_wrong_context_rejected\":{},",
                "\"allocated\":{},",
                "\"reused\":{},",
                "\"cache_hits_delta\":{},",
                "\"cache_inserts_delta\":{},",
                "\"cache_bypasses_delta\":{},",
                "\"pac_signs_delta\":{},",
                "\"pac_verifications_delta\":{},",
                "\"pac_failures_delta\":{},",
                "\"pac_software_fallback_signs_delta\":{},",
                "\"pac_software_fallback_verifications_delta\":{},",
                "\"pac_software_fallback_failures_delta\":{}",
                "}}"
            ),
            cfg!(feature = "pac"),
            cfg!(target_arch = "aarch64"),
            probe.attempted,
            probe.pac_probe_uses_allocated_object,
            probe.context_binding_active,
            probe.software_fallback_active,
            probe.pac_probe_key,
            probe.pac_probe_active_key,
            probe.pac_probe_key_matrix_keys[0],
            probe.pac_probe_key_matrix_available[0],
            probe.pac_probe_key_matrix_signed_changed[0],
            probe.pac_probe_key_matrix_strip_roundtrip[0],
            probe.pac_probe_key_matrix_correct_context_roundtrip[0],
            probe.pac_probe_key_matrix_wrong_context_rejected[0],
            probe.pac_probe_key_matrix_keys[1],
            probe.pac_probe_key_matrix_available[1],
            probe.pac_probe_key_matrix_signed_changed[1],
            probe.pac_probe_key_matrix_strip_roundtrip[1],
            probe.pac_probe_key_matrix_correct_context_roundtrip[1],
            probe.pac_probe_key_matrix_wrong_context_rejected[1],
            probe.pac_probe_key_matrix_keys[2],
            probe.pac_probe_key_matrix_available[2],
            probe.pac_probe_key_matrix_signed_changed[2],
            probe.pac_probe_key_matrix_strip_roundtrip[2],
            probe.pac_probe_key_matrix_correct_context_roundtrip[2],
            probe.pac_probe_key_matrix_wrong_context_rejected[2],
            probe.pac_probe_key_matrix_keys[3],
            probe.pac_probe_key_matrix_available[3],
            probe.pac_probe_key_matrix_signed_changed[3],
            probe.pac_probe_key_matrix_strip_roundtrip[3],
            probe.pac_probe_key_matrix_correct_context_roundtrip[3],
            probe.pac_probe_key_matrix_wrong_context_rejected[3],
            probe.pac_probe_signed_changed,
            probe.pac_probe_strip_roundtrip,
            probe.pac_probe_correct_context_roundtrip,
            probe.pac_probe_wrong_context_rejected,
            probe.allocated,
            probe.reused,
            probe.cache_hits_delta,
            probe.cache_inserts_delta,
            probe.cache_bypasses_delta,
            probe.pac_signs_delta,
            probe.pac_verifications_delta,
            probe.pac_failures_delta,
            probe.pac_software_fallback_signs_delta,
            probe.pac_software_fallback_verifications_delta,
            probe.pac_software_fallback_failures_delta
        );
    }

    b.iter(|| test::black_box(()));
}

#[cfg(all(
    not(feature = "stats"),
    any(
        feature = "type_isolation",
        feature = "metadata_segregation",
        feature = "hugepage",
        feature = "pac",
        feature = "mte",
        feature = "mpk",
        feature = "guard_pages",
        feature = "quarantine",
        feature = "force_initialize"
    )
))]
#[bench]
fn zzz_semantic_auto_metadata_report(b: &mut test::Bencher) {
    let auto_type_id_basis = semantic_auto_metadata_type_id_basis();
    let type_id_basis = if std_bench_mir_semantic_scope_runtime_mode() {
        RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_TYPE_ID_BASIS
    } else {
        auto_type_id_basis
    };
    let compiler_site_replay =
        !std_bench_mir_semantic_scope_runtime_mode() && type_id_basis.contains("compiler-assigned");
    let compiler_site_id_stream_mode = if std_bench_mir_semantic_scope_runtime_mode() {
        RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_STREAM_MODE
    } else if type_id_basis.contains("consuming-stream") {
        "consuming-stream"
    } else if type_id_basis.contains("cyclic-replay") {
        "cyclic-replay"
    } else {
        "none"
    };
    let policy_flags = std_bench_auto_policy_flags();

    semantic_auto_metadata_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"std_bench_auto_metadata\",",
            "\"event\":\"semantic_harness_state\",",
            "\"stats_feature\":false,",
            "\"type_id_basis\":\"{}\",",
            "\"compiler_site_replay\":{},",
            "\"compiler_site_id_stream_mode\":\"{}\",",
            "\"policy_flags_seen\":{},",
            "\"feature_type_isolation\":{},",
            "\"feature_metadata_segregation\":{},",
            "\"feature_hugepage\":{},",
            "\"feature_pac\":{}",
            "}}"
        ),
        type_id_basis,
        compiler_site_replay,
        compiler_site_id_stream_mode,
        policy_flags,
        cfg!(feature = "type_isolation"),
        cfg!(feature = "metadata_segregation"),
        cfg!(feature = "hugepage"),
        cfg!(feature = "pac")
    );

    b.iter(|| test::black_box(()));
}
