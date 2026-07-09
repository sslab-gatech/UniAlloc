#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define UNIALLOC_SEMANTIC_STATS_SNAPSHOT_ABI_VERSION 3u
#define UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION 2u
#define UNIALLOC_SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION 1u
#define UNIALLOC_SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION 1u
#define UNIALLOC_CONSTRAINED_BOOT_SAMPLE_ABI_VERSION 2u
#define UNIALLOC_FLAG_TYPE_ISOLATED (1u << 0)

typedef struct UniallocSemanticStatsSnapshot {
    size_t total_allocations;
    size_t typed_allocations;
    size_t fallback_allocations;
    size_t total_allocated_bytes;
    size_t typed_allocated_bytes;
    size_t fallback_allocated_bytes;
    size_t typed_deallocations;
    size_t fallback_deallocations;
    uint32_t policy_flags_seen;
    uint64_t last_type_id;
    size_t coverage_basis_points;
    size_t typed_cache_hits;
    size_t typed_cache_inserts;
    size_t typed_cache_bypasses;
    size_t delayed_free_enqueues;
    size_t delayed_free_flushes;
    size_t metadata_pac_auth_signs;
    size_t metadata_pac_auth_verifications;
    size_t metadata_pac_auth_failures;
    size_t metadata_pac_software_fallback_signs;
    size_t metadata_pac_software_fallback_verifications;
    size_t metadata_pac_software_fallback_failures;
    size_t total_deallocations;
    size_t semantic_type_stats_dropped_events;
} UniallocSemanticStatsSnapshot;

typedef struct UniallocSemanticTypeStatsSnapshot {
    uint64_t type_id;
    uint64_t module_id;
    uint64_t callsite;
    size_t allocations;
    size_t allocated_bytes;
    size_t deallocations;
    size_t cache_hits;
    size_t cache_inserts;
    size_t cache_bypasses;
    size_t observed_alloc_size;
    size_t observed_alloc_align;
    size_t observed_dealloc_size;
    size_t observed_dealloc_align;
    uint32_t policy_flags_seen;
} UniallocSemanticTypeStatsSnapshot;

typedef struct UniallocSemanticFallbackAttributionSnapshot {
    size_t raw_alloc_no_metadata;
    size_t raw_alloc_no_metadata_bytes;
    size_t raw_dealloc_no_metadata;
    size_t raw_realloc_no_metadata;
    size_t raw_realloc_no_metadata_bytes;
    size_t raw_realloc_moved_dealloc_no_metadata;
    size_t realloc_recorded_old_metadata_new_allocations;
    size_t realloc_recorded_old_metadata_new_allocation_bytes;
} UniallocSemanticFallbackAttributionSnapshot;

typedef struct UniallocSemanticMetadataValidationSnapshot {
    size_t recovery_identity_matches;
    size_t recovery_identity_mismatches;
    uint64_t last_mismatch_requested_type_id;
    uint64_t last_mismatch_recorded_type_id;
    uint64_t last_mismatch_requested_module_id;
    uint64_t last_mismatch_recorded_module_id;
    uint64_t last_mismatch_requested_callsite;
    uint64_t last_mismatch_recorded_callsite;
} UniallocSemanticMetadataValidationSnapshot;

typedef struct UniallocConstrainedBootSample {
    size_t boot_cycle;
    bool fixed_heap_ready;
    bool c_abi_invoked;
    bool c_abi_ready_after_init;
    size_t c_abi_round_trips;
    size_t c_abi_invalid_layouts_rejected;
    bool c_abi_over_page_alignment_checked;
    size_t c_abi_over_page_alignment;
    size_t allocator_total_allocations;
    size_t allocator_total_deallocations;
    size_t allocator_typed_allocations;
    size_t allocator_typed_deallocations;
    size_t allocator_fallback_allocations;
    size_t allocator_fallback_deallocations;
    size_t allocator_total_allocated_bytes;
    size_t allocator_typed_allocated_bytes;
    size_t allocator_fallback_allocated_bytes;
    size_t allocator_coverage_basis_points;
    size_t allocator_type_stats_rows;
    size_t allocator_type_stats_dropped_events;
    bool allocator_type_stats_probe_matched;
} UniallocConstrainedBootSample;

bool unialloc_fixed_heap_try_init(size_t heap_start, size_t heap_size, size_t page_size);
void unialloc_fixed_heap_extend(size_t size, size_t page_size);
bool unialloc_fixed_heap_ready(void);
bool unialloc_fixed_heap_try_extend(size_t size, size_t page_size);

void *unialloc_alloc(size_t size, size_t align);
void unialloc_dealloc(void *ptr, size_t size, size_t align);
void *unialloc_realloc(void *ptr, size_t old_size, size_t old_align, size_t new_size);

void *__unialloc_alloc_with_metadata(
    size_t size,
    size_t align,
    uint64_t type_id,
    uint64_t module_id,
    uint32_t flags,
    uint64_t callsite);
bool __unialloc_dealloc_with_metadata(
    void *ptr,
    size_t size,
    size_t align,
    uint64_t type_id,
    uint64_t module_id,
    uint32_t flags,
    uint64_t callsite);

uint32_t __unialloc_semantic_stats_snapshot_abi_version(void);
size_t __unialloc_semantic_stats_snapshot_size(void);
bool __unialloc_semantic_stats_snapshot_checked(
    UniallocSemanticStatsSnapshot *out,
    size_t out_size);
bool __unialloc_semantic_stats_snapshot(UniallocSemanticStatsSnapshot *out);
void __unialloc_semantic_stats_reset(void);

uint32_t __unialloc_semantic_type_stats_snapshot_abi_version(void);
size_t __unialloc_semantic_type_stats_snapshot_record_size(void);
size_t __unialloc_semantic_type_stats_snapshot_checked(
    UniallocSemanticTypeStatsSnapshot *out,
    size_t len,
    size_t record_size);
size_t __unialloc_semantic_type_stats_snapshot(
    UniallocSemanticTypeStatsSnapshot *out,
    size_t len);

uint32_t __unialloc_semantic_fallback_attribution_snapshot_abi_version(void);
size_t __unialloc_semantic_fallback_attribution_snapshot_size(void);
bool __unialloc_semantic_fallback_attribution_snapshot_checked(
    UniallocSemanticFallbackAttributionSnapshot *out,
    size_t out_size);
bool __unialloc_semantic_fallback_attribution_snapshot(
    UniallocSemanticFallbackAttributionSnapshot *out);

uint32_t __unialloc_semantic_metadata_validation_snapshot_abi_version(void);
size_t __unialloc_semantic_metadata_validation_snapshot_size(void);
bool __unialloc_semantic_metadata_validation_snapshot_checked(
    UniallocSemanticMetadataValidationSnapshot *out,
    size_t out_size);
bool __unialloc_semantic_metadata_validation_snapshot(
    UniallocSemanticMetadataValidationSnapshot *out);

uint32_t __unialloc_constrained_boot_sample_abi_version(void);
size_t __unialloc_constrained_boot_sample_size(void);
bool __unialloc_constrained_boot_sample_checked(
    UniallocConstrainedBootSample *out,
    size_t out_size,
    size_t boot_cycle,
    bool c_abi_invoked,
    bool c_abi_ready_after_init,
    size_t c_abi_round_trips,
    size_t c_abi_invalid_layouts_rejected,
    bool c_abi_over_page_alignment_checked,
    size_t c_abi_over_page_alignment,
    size_t allocator_type_stats_rows,
    bool allocator_type_stats_probe_matched);
