#![feature(layout_for_ptr)]
#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]

use std::alloc::{alloc, alloc_zeroed, dealloc, handle_alloc_error, realloc, GlobalAlloc, Layout};
use std::boxed::Box;
use std::mem::{align_of, size_of};
use std::ptr::{read_volatile, write_volatile};

use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot, semantic_stats_reset,
    semantic_stats_snapshot, semantic_type_stats_snapshot, SemanticMetadataValidationSnapshot,
    SemanticTypeStatsSnapshot, UniAlloc,
};

#[cfg(feature = "fixed_heap")]
#[path = "../bin_support/fixed_heap_probe_global.rs"]
mod fixed_heap_probe_global;

#[cfg(feature = "fixed_heap")]
#[global_allocator]
static A: fixed_heap_probe_global::FixedHeapProbeAllocator =
    fixed_heap_probe_global::FixedHeapProbeAllocator;

#[cfg(not(feature = "fixed_heap"))]
#[global_allocator]
static A: UniAlloc = UniAlloc;

#[cfg(feature = "fixed_heap")]
static DIRECT_ALLOCATOR_RECEIVER: UniAlloc = UniAlloc;

#[cfg(feature = "fixed_heap")]
#[inline(always)]
fn direct_allocator_receiver() -> &'static UniAlloc {
    &DIRECT_ALLOCATOR_RECEIVER
}

#[cfg(not(feature = "fixed_heap"))]
#[inline(always)]
fn direct_allocator_receiver() -> &'static UniAlloc {
    &A
}

const RUSTC_DRIVER_MIR_REWRITE_MODULE_ID: u64 = 0xC002_DA00_0000_0001;

#[inline(never)]
fn consume_box(value: Box<[u64; 16]>) -> u64 {
    let mut checksum = 0u64;
    for item in value.iter() {
        checksum = checksum.rotate_left(5) ^ *item;
    }
    checksum
}

#[inline(never)]
fn run_direct_allocator_workload(rounds: usize) -> u64 {
    let mut checksum = 0xC002_DA11_EC70_A110u64;
    for round in 0..rounds {
        let mut value = Box::new([0u64; 16]);
        for (index, slot) in value.iter_mut().enumerate() {
            *slot = (round as u64).rotate_left(7) ^ ((index as u64) << 17) ^ 0xA110_C002;
        }
        checksum = checksum.rotate_left(3) ^ consume_box(value);
    }
    checksum
}

#[inline(never)]
unsafe fn run_layout_allocator_workload(rounds: usize) -> u64 {
    let mut checksum = 0xC002_1A70_0000_0001u64;
    for round in 0..rounds {
        let old_layout = Layout::array::<u64>(16).expect("valid old layout");
        let old_ptr = alloc(old_layout);
        if old_ptr.is_null() {
            handle_alloc_error(old_layout);
        }

        let old_words = old_ptr as *mut u64;
        for index in 0..16usize {
            let value = (round as u64).rotate_left(11) ^ ((index as u64) << 9) ^ 0xA110_1A70_C002;
            write_volatile(old_words.add(index), value);
            checksum = checksum.rotate_left(7) ^ read_volatile(old_words.add(index));
        }

        let new_layout = Layout::array::<u64>(32).expect("valid new layout");
        let grown_ptr = realloc(old_ptr, old_layout, new_layout.size());
        if grown_ptr.is_null() {
            handle_alloc_error(new_layout);
        }

        let grown_words = grown_ptr as *mut u64;
        for index in 16..32usize {
            let value = (round as u64).rotate_left(17) ^ ((index as u64) << 5) ^ 0x1A70_A110_C002;
            write_volatile(grown_words.add(index), value);
            checksum = checksum.rotate_left(3) ^ read_volatile(grown_words.add(index));
        }

        dealloc(grown_ptr, new_layout);

        let zeroed_layout = Layout::array::<u64>(8).expect("valid zeroed layout");
        let zeroed_ptr = alloc_zeroed(zeroed_layout);
        if zeroed_ptr.is_null() {
            handle_alloc_error(zeroed_layout);
        }
        let zeroed_words = zeroed_ptr as *mut u64;
        for index in 0..8usize {
            let observed = read_volatile(zeroed_words.add(index));
            assert_eq!(
                observed, 0,
                "alloc_zeroed MIR probe returned non-zero memory"
            );
            let value = (round as u64).rotate_left(19) ^ ((index as u64) << 13) ^ 0xA110_2E80_C002;
            write_volatile(zeroed_words.add(index), value);
            checksum = checksum.rotate_left(11) ^ read_volatile(zeroed_words.add(index));
        }
        dealloc(zeroed_ptr, zeroed_layout);

        let runtime_len = 6usize + (round % 3);
        let runtime_array_layout =
            Layout::array::<u32>(runtime_len).expect("valid runtime-length array layout");
        let runtime_array_ptr = alloc(runtime_array_layout);
        if runtime_array_ptr.is_null() {
            handle_alloc_error(runtime_array_layout);
        }
        let runtime_array_words = runtime_array_ptr as *mut u32;
        for index in 0..runtime_len {
            let value = (round as u32).rotate_left((index % 17) as u32)
                ^ (index as u32).wrapping_mul(23)
                ^ 0xA110_C002u32;
            write_volatile(runtime_array_words.add(index), value);
            checksum =
                checksum.rotate_left(5) ^ (read_volatile(runtime_array_words.add(index)) as u64);
        }
        dealloc(runtime_array_ptr, runtime_array_layout);

        let scalar_layout = Layout::new::<u128>();
        let scalar_ptr = alloc(scalar_layout);
        if scalar_ptr.is_null() {
            handle_alloc_error(scalar_layout);
        }
        let scalar_words = scalar_ptr as *mut u128;
        let scalar_value =
            ((round as u128) << 72) ^ ((round as u128).rotate_left(23)) ^ 0xC002_A110_1A70_u128;
        write_volatile(scalar_words, scalar_value);
        let observed_scalar = read_volatile(scalar_words);
        checksum =
            checksum.rotate_left(13) ^ (observed_scalar as u64) ^ ((observed_scalar >> 64) as u64);
        dealloc(scalar_ptr, scalar_layout);

        let transformed_layout = Layout::new::<[u128; 3]>()
            .align_to(64)
            .expect("valid transformed layout alignment")
            .pad_to_align();
        let transformed_ptr = alloc(transformed_layout);
        if transformed_ptr.is_null() {
            handle_alloc_error(transformed_layout);
        }
        let transformed_words = transformed_ptr as *mut u128;
        for index in 0..3usize {
            let value =
                ((round as u128) << 80) ^ ((index as u128) << 32) ^ 0xC002_A110_1A70_7A11_u128;
            write_volatile(transformed_words.add(index), value);
            let observed = read_volatile(transformed_words.add(index));
            checksum = checksum.rotate_left(17) ^ (observed as u64) ^ ((observed >> 64) as u64);
        }
        dealloc(transformed_ptr, transformed_layout);

        let option_wrapped_layout = Layout::new::<[u32; 7]>()
            .align_to(32)
            .ok()
            .expect("valid option-wrapped transformed layout");
        let option_wrapped_ptr = alloc(option_wrapped_layout);
        if option_wrapped_ptr.is_null() {
            handle_alloc_error(option_wrapped_layout);
        }
        let option_wrapped_words = option_wrapped_ptr as *mut u32;
        for index in 0..7usize {
            let value = (round as u32).rotate_left((index % 19) as u32)
                ^ (index as u32).wrapping_mul(37)
                ^ 0x0F71_0C02u32;
            write_volatile(option_wrapped_words.add(index), value);
            checksum =
                checksum.rotate_left(31) ^ (read_volatile(option_wrapped_words.add(index)) as u64);
        }
        dealloc(option_wrapped_ptr, option_wrapped_layout);

        let (extended_layout, extended_tail_offset) = Layout::new::<u32>()
            .extend(Layout::new::<u64>())
            .expect("valid extended composite layout");
        let extended_ptr = alloc(extended_layout);
        if extended_ptr.is_null() {
            handle_alloc_error(extended_layout);
        }
        let extended_head = extended_ptr as *mut u32;
        let extended_tail = extended_ptr.add(extended_tail_offset) as *mut u64;
        let extended_head_value =
            (round as u32).rotate_left(5) ^ 0xC002_A110u32 ^ (extended_tail_offset as u32);
        let extended_tail_value = (round as u64).rotate_left(41) ^ 0xC002_A110_1A70_EE77u64;
        write_volatile(extended_head, extended_head_value);
        write_volatile(extended_tail, extended_tail_value);
        checksum = checksum.rotate_left(29)
            ^ (read_volatile(extended_head) as u64)
            ^ read_volatile(extended_tail);
        dealloc(extended_ptr, extended_layout);

        let ref_projected_pair = Layout::new::<u8>()
            .extend(Layout::new::<[u64; 2]>())
            .expect("valid ref-projected composite layout");
        let ref_projected_pair_ref = &ref_projected_pair;
        let ref_projected_layout = ref_projected_pair_ref.0.pad_to_align();
        let ref_projected_ptr = alloc(ref_projected_layout);
        if ref_projected_ptr.is_null() {
            handle_alloc_error(ref_projected_layout);
        }
        for index in 0..ref_projected_layout.size() {
            let value = (round as u8).rotate_left((index % 5) as u32)
                ^ (index as u8).wrapping_mul(29)
                ^ 0xC2u8;
            write_volatile(ref_projected_ptr.add(index), value);
            checksum =
                checksum.rotate_left(11) ^ (read_volatile(ref_projected_ptr.add(index)) as u64);
        }
        dealloc(ref_projected_ptr, ref_projected_layout);

        let (repeated_layout, repeated_stride) = Layout::new::<u16>()
            .repeat(5)
            .expect("valid repeated composite layout");
        let repeated_ptr = alloc(repeated_layout);
        if repeated_ptr.is_null() {
            handle_alloc_error(repeated_layout);
        }
        for index in 0..5usize {
            let slot = repeated_ptr.add(index * repeated_stride) as *mut u16;
            let value = (round as u16).rotate_left((index % 11) as u32)
                ^ (index as u16).wrapping_mul(31)
                ^ 0x7A11u16;
            write_volatile(slot, value);
            checksum = checksum.rotate_left(7) ^ (read_volatile(slot) as u64);
        }
        dealloc(repeated_ptr, repeated_layout);

        let source_layout = Layout::new::<[u8; 96]>();
        let reconstructed_layout =
            Layout::from_size_align(source_layout.size(), source_layout.align())
                .expect("valid same-source reconstructed layout");
        let reconstructed_ptr = alloc(reconstructed_layout);
        if reconstructed_ptr.is_null() {
            handle_alloc_error(reconstructed_layout);
        }
        for index in 0..reconstructed_layout.size() {
            let value = (round as u8).rotate_left((index % 7) as u32) ^ (index as u8) ^ 0x5Au8;
            write_volatile(reconstructed_ptr.add(index), value);
            checksum =
                checksum.rotate_left(3) ^ (read_volatile(reconstructed_ptr.add(index)) as u64);
        }
        dealloc(reconstructed_ptr, reconstructed_layout);

        let size_align_layout =
            Layout::from_size_align(size_of::<[u16; 11]>(), align_of::<[u16; 11]>())
                .expect("valid size_of/align_of reconstructed layout");
        let size_align_ptr = alloc(size_align_layout);
        if size_align_ptr.is_null() {
            handle_alloc_error(size_align_layout);
        }
        let size_align_words = size_align_ptr as *mut u16;
        for index in 0..11usize {
            let value = (round as u16).rotate_left((index % 13) as u32)
                ^ (index as u16).wrapping_mul(19)
                ^ 0x51A1u16;
            write_volatile(size_align_words.add(index), value);
            checksum =
                checksum.rotate_left(13) ^ (read_volatile(size_align_words.add(index)) as u64);
        }
        dealloc(size_align_ptr, size_align_layout);

        let packed_extended_layout = Layout::new::<u32>()
            .extend_packed(Layout::new::<u64>())
            .expect("valid packed extended composite layout");
        let packed_extended_ptr = alloc(packed_extended_layout);
        if packed_extended_ptr.is_null() {
            handle_alloc_error(packed_extended_layout);
        }
        for index in 0..packed_extended_layout.size() {
            let value = (round as u8).rotate_left((index % 5) as u32)
                ^ (index as u8).wrapping_mul(13)
                ^ 0xA7u8;
            write_volatile(packed_extended_ptr.add(index), value);
            checksum =
                checksum.rotate_left(11) ^ (read_volatile(packed_extended_ptr.add(index)) as u64);
        }
        dealloc(packed_extended_ptr, packed_extended_layout);

        let packed_repeated_layout = Layout::new::<u32>()
            .repeat_packed(4)
            .expect("valid packed repeated composite layout");
        let packed_repeated_ptr = alloc(packed_repeated_layout);
        if packed_repeated_ptr.is_null() {
            handle_alloc_error(packed_repeated_layout);
        }
        for index in 0..packed_repeated_layout.size() {
            let value = (round as u8).rotate_left((index % 3) as u32)
                ^ (index as u8).wrapping_mul(17)
                ^ 0x3Du8;
            write_volatile(packed_repeated_ptr.add(index), value);
            checksum =
                checksum.rotate_left(19) ^ (read_volatile(packed_repeated_ptr.add(index)) as u64);
        }
        dealloc(packed_repeated_ptr, packed_repeated_layout);

        let stack_array = [
            round as u64,
            (round as u64).rotate_left(29) ^ 0xF042_A110,
            checksum.rotate_left(31),
            checksum ^ 0xC002_F042_A110,
        ];
        let for_value_layout = Layout::for_value(&stack_array);
        let for_value_ptr = alloc(for_value_layout);
        if for_value_ptr.is_null() {
            handle_alloc_error(for_value_layout);
        }
        let for_value_words = for_value_ptr as *mut u64;
        for (index, value) in stack_array.iter().copied().enumerate() {
            write_volatile(for_value_words.add(index), value);
            checksum = checksum.rotate_left(23) ^ read_volatile(for_value_words.add(index));
        }
        dealloc(for_value_ptr, for_value_layout);

        let raw_slice: *const [u64] = (&stack_array[..]) as *const [u64];
        let raw_value_layout = Layout::for_value_raw(raw_slice);
        let raw_value_ptr = alloc(raw_value_layout);
        if raw_value_ptr.is_null() {
            handle_alloc_error(raw_value_layout);
        }
        let raw_value_words = raw_value_ptr as *mut u64;
        for (index, value) in stack_array.iter().copied().enumerate() {
            let raw_value = value.rotate_left((index as u32) + 1) ^ 0xF042_C002_A110u64;
            write_volatile(raw_value_words.add(index), raw_value);
            checksum = checksum.rotate_left(27) ^ read_volatile(raw_value_words.add(index));
        }
        dealloc(raw_value_ptr, raw_value_layout);

        let global_receiver = direct_allocator_receiver();
        let global_zeroed_layout = Layout::array::<u16>(10).expect("valid GlobalAlloc layout");
        let global_zeroed = GlobalAlloc::alloc_zeroed(global_receiver, global_zeroed_layout);
        if global_zeroed.is_null() {
            handle_alloc_error(global_zeroed_layout);
        }
        let global_zeroed_words = global_zeroed as *mut u16;
        for index in 0..10usize {
            assert_eq!(
                read_volatile(global_zeroed_words.add(index)),
                0,
                "GlobalAlloc::alloc_zeroed MIR probe returned non-zero memory"
            );
            let value = (round as u16).rotate_left((index % 13) as u32) ^ (index as u16);
            write_volatile(global_zeroed_words.add(index), value);
            checksum =
                checksum.rotate_left(5) ^ (read_volatile(global_zeroed_words.add(index)) as u64);
        }
        GlobalAlloc::dealloc(global_receiver, global_zeroed, global_zeroed_layout);

        let global_old_layout = Layout::array::<u32>(4).expect("valid GlobalAlloc old layout");
        let global_new_layout = Layout::array::<u32>(8).expect("valid GlobalAlloc new layout");
        let global_ptr = GlobalAlloc::alloc(global_receiver, global_old_layout);
        if global_ptr.is_null() {
            handle_alloc_error(global_old_layout);
        }
        let global_words = global_ptr as *mut u32;
        for index in 0..4usize {
            let value = (round as u32).rotate_left(3) ^ ((index as u32) << 11) ^ 0xC002_A110;
            write_volatile(global_words.add(index), value);
            checksum = checksum.rotate_left(7) ^ (read_volatile(global_words.add(index)) as u64);
        }
        let global_grown = GlobalAlloc::realloc(
            global_receiver,
            global_ptr,
            global_old_layout,
            global_new_layout.size(),
        );
        if global_grown.is_null() {
            handle_alloc_error(global_new_layout);
        }
        let global_grown_words = global_grown as *mut u32;
        for index in 4..8usize {
            let value = (round as u32).rotate_left(9) ^ ((index as u32) << 7) ^ 0xA110_C002;
            write_volatile(global_grown_words.add(index), value);
            checksum =
                checksum.rotate_left(17) ^ (read_volatile(global_grown_words.add(index)) as u64);
        }
        GlobalAlloc::dealloc(global_receiver, global_grown, global_new_layout);
    }
    checksum
}

fn empty_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

fn semantic_metadata_validation_json(snapshot: SemanticMetadataValidationSnapshot) -> String {
    format!(
        concat!(
            "{{",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"last_mismatch_requested_module_id\":{},",
            "\"last_mismatch_recorded_module_id\":{},",
            "\"last_mismatch_requested_callsite\":{},",
            "\"last_mismatch_recorded_callsite\":{}",
            "}}"
        ),
        snapshot.recovery_identity_matches,
        snapshot.recovery_identity_mismatches,
        snapshot.last_mismatch_requested_type_id,
        snapshot.last_mismatch_recorded_type_id,
        snapshot.last_mismatch_requested_module_id,
        snapshot.last_mismatch_recorded_module_id,
        snapshot.last_mismatch_requested_callsite,
        snapshot.last_mismatch_recorded_callsite
    )
}

fn main() {
    // This example intentionally uses ordinary Box allocations only: no manual
    // metadata allocation ABI calls and no semantic metadata scope.  A passing
    // run therefore requires the rustc_driver optimized-MIR provider override
    // to rewrite the Box allocation call to UniAlloc's metadata ABI.
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let mut checksum = run_direct_allocator_workload(8);
    unsafe {
        checksum ^= run_layout_allocator_workload(4);
    }
    assert_ne!(checksum, 0, "direct allocator MIR probe must run real work");

    let snap = semantic_stats_snapshot();
    let mut rows = [empty_type_stats_row(); 64];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    let lowered_rows = rows
        .iter()
        .take(std::cmp::min(row_count, rows.len()))
        .filter(|row| row.allocations > 0 && row.module_id == RUSTC_DRIVER_MIR_REWRITE_MODULE_ID)
        .count();

    assert!(
        snap.typed_allocations > 0,
        "direct allocator MIR rewrite did not produce typed allocations: {:?}",
        snap
    );
    assert!(
        snap.typed_deallocations > 0,
        "ordinary Box drops did not recover compiler-emitted metadata: {:?}",
        snap
    );
    assert!(
        lowered_rows > 0,
        "no typed stats row used the rustc_driver direct allocator module"
    );

    let mut runtime_layout_observation_rows = rows
        .iter()
        .take(std::cmp::min(row_count, rows.len()))
        .filter(|row| {
            let has_alloc_layout_evidence =
                row.allocations > 0 && row.observed_alloc_size > 0 && row.observed_alloc_align > 0;
            let has_dealloc_layout_evidence = row.deallocations > 0
                && row.observed_dealloc_size > 0
                && row.observed_dealloc_align > 0;

            // Local size/align metadata ABI rewriting has separate MIR callsites
            // for allocation and deallocation, so alloc-only and dealloc-only
            // rows are real runtime provenance and must be emitted. Realloc
            // callsites still naturally carry evidence for both sides.
            row.module_id == RUSTC_DRIVER_MIR_REWRITE_MODULE_ID
                && (has_alloc_layout_evidence || has_dealloc_layout_evidence)
        })
        .collect::<Vec<_>>();
    runtime_layout_observation_rows.sort_by_key(|row| (row.module_id, row.callsite, row.type_id));

    let runtime_layout_observation_rows = runtime_layout_observation_rows
        .iter()
        .map(|row| {
            format!(
                concat!(
                    "{{",
                    "\"type_id\":{},",
                    "\"module_id\":{},",
                    "\"callsite\":{},",
                    "\"allocations\":{},",
                    "\"deallocations\":{},",
                    "\"allocated_bytes\":{},",
                    "\"observed_alloc_size\":{},",
                    "\"observed_alloc_align\":{},",
                    "\"observed_dealloc_size\":{},",
                    "\"observed_dealloc_align\":{}",
                    "}}"
                ),
                row.type_id,
                row.module_id,
                row.callsite,
                row.allocations,
                row.deallocations,
                row.allocated_bytes,
                row.observed_alloc_size,
                row.observed_alloc_align,
                row.observed_dealloc_size,
                row.observed_dealloc_align
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let semantic_metadata_validation = semantic_metadata_validation_snapshot();
    let semantic_metadata_validation_json =
        semantic_metadata_validation_json(semantic_metadata_validation);

    println!(
        concat!(
            "{{",
            "\"source\":\"rustc_driver_direct_allocator_mir_probe\",",
            "\"type_id_basis\":\"compiler-assigned-allocation-site-object-type-id-rustc-driver-direct-allocator-mir\",",
            "\"compiler_site_id_stream_mode\":\"rustc-driver-direct-allocator-mir\",",
            "\"compiler_site_replay\":false,",
            "\"manual_metadata_abi_calls\":false,",
            "\"checksum\":{},",
            "\"total_allocations\":{},",
            "\"typed_allocations\":{},",
            "\"fallback_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"policy_flags_seen\":{},",
            "\"lowered_rows\":{},",
            "\"semantic_metadata_validation\":{},",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"last_mismatch_requested_module_id\":{},",
            "\"last_mismatch_recorded_module_id\":{},",
            "\"last_mismatch_requested_callsite\":{},",
            "\"last_mismatch_recorded_callsite\":{},",
            "\"runtime_layout_observation_rows\":[{}]",
            "}}"
        ),
        checksum,
        snap.total_allocations,
        snap.typed_allocations,
        snap.fallback_allocations,
        snap.typed_deallocations,
        snap.policy_flags_seen,
        lowered_rows,
        semantic_metadata_validation_json,
        semantic_metadata_validation.recovery_identity_matches,
        semantic_metadata_validation.recovery_identity_mismatches,
        semantic_metadata_validation.last_mismatch_requested_type_id,
        semantic_metadata_validation.last_mismatch_recorded_type_id,
        semantic_metadata_validation.last_mismatch_requested_module_id,
        semantic_metadata_validation.last_mismatch_recorded_module_id,
        semantic_metadata_validation.last_mismatch_requested_callsite,
        semantic_metadata_validation.last_mismatch_recorded_callsite,
        runtime_layout_observation_rows
    );
}
