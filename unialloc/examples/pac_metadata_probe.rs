use std::alloc::Layout;

use unialloc::alloc_api::{FLAG_METADATA_PROTECTION, FLAG_POINTER_AUTH};
use unialloc::{
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    AllocationMetadata, SemanticAlloc, UniAlloc, FLAG_TYPE_ISOLATED,
};

#[cfg(target_arch = "aarch64")]
#[derive(Clone, Copy)]
struct PacProbeSnapshot {
    key: &'static str,
    active_key: &'static str,
    context_binding_active: bool,
    signed_changed: bool,
    strip_roundtrip: bool,
    correct_context_roundtrip: bool,
    wrong_context_rejected: bool,
    matrix_keys: [&'static str; 4],
    matrix_available: [bool; 4],
    matrix_signed_changed: [bool; 4],
    matrix_strip_roundtrip: [bool; 4],
    matrix_correct_context_roundtrip: [bool; 4],
    matrix_wrong_context_rejected: [bool; 4],
}

#[cfg(not(target_arch = "aarch64"))]
#[derive(Clone, Copy)]
struct PacProbeSnapshot {
    key: &'static str,
    active_key: &'static str,
    context_binding_active: bool,
    signed_changed: bool,
    strip_roundtrip: bool,
    correct_context_roundtrip: bool,
    wrong_context_rejected: bool,
    matrix_keys: [&'static str; 4],
    matrix_available: [bool; 4],
    matrix_signed_changed: [bool; 4],
    matrix_strip_roundtrip: [bool; 4],
    matrix_correct_context_roundtrip: [bool; 4],
    matrix_wrong_context_rejected: [bool; 4],
}

#[cfg(target_arch = "aarch64")]
fn pac_probe_snapshot(ptr: usize) -> PacProbeSnapshot {
    let probe = unialloc::pac::best_context_binding_probe_for(ptr);
    let matrix = unialloc::pac::context_binding_probe_matrix_for(ptr);
    PacProbeSnapshot {
        key: probe.key.as_str(),
        active_key: unialloc::pac::active_context_binding_key_name(),
        context_binding_active: unialloc::pac::context_binding_available_for(ptr),
        signed_changed: probe.signed_changed,
        strip_roundtrip: probe.strip_roundtrip,
        correct_context_roundtrip: probe.correct_context_roundtrip,
        wrong_context_rejected: probe.wrong_context_rejected,
        matrix_keys: [
            matrix[0].key.as_str(),
            matrix[1].key.as_str(),
            matrix[2].key.as_str(),
            matrix[3].key.as_str(),
        ],
        matrix_available: [
            matrix[0].available,
            matrix[1].available,
            matrix[2].available,
            matrix[3].available,
        ],
        matrix_signed_changed: [
            matrix[0].signed_changed,
            matrix[1].signed_changed,
            matrix[2].signed_changed,
            matrix[3].signed_changed,
        ],
        matrix_strip_roundtrip: [
            matrix[0].strip_roundtrip,
            matrix[1].strip_roundtrip,
            matrix[2].strip_roundtrip,
            matrix[3].strip_roundtrip,
        ],
        matrix_correct_context_roundtrip: [
            matrix[0].correct_context_roundtrip,
            matrix[1].correct_context_roundtrip,
            matrix[2].correct_context_roundtrip,
            matrix[3].correct_context_roundtrip,
        ],
        matrix_wrong_context_rejected: [
            matrix[0].wrong_context_rejected,
            matrix[1].wrong_context_rejected,
            matrix[2].wrong_context_rejected,
            matrix[3].wrong_context_rejected,
        ],
    }
}

#[cfg(not(target_arch = "aarch64"))]
fn pac_probe_snapshot(_ptr: usize) -> PacProbeSnapshot {
    PacProbeSnapshot {
        key: "none",
        active_key: "none",
        context_binding_active: false,
        signed_changed: false,
        strip_roundtrip: false,
        correct_context_roundtrip: false,
        wrong_context_rejected: false,
        matrix_keys: ["none", "none", "none", "none"],
        matrix_available: [false; 4],
        matrix_signed_changed: [false; 4],
        matrix_strip_roundtrip: [false; 4],
        matrix_correct_context_roundtrip: [false; 4],
        matrix_wrong_context_rejected: [false; 4],
    }
}

fn json_bool(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn main() {
    semantic_stats_reset();
    let alloc = UniAlloc::new();
    let layout = Layout::from_size_align(
        4 * std::mem::size_of::<usize>(),
        std::mem::align_of::<usize>(),
    )
    .expect("valid probe layout");
    let metadata = AllocationMetadata::for_type(0x5041_434d_4554_4101)
        .with_module(0x5041_434d_4f44_0001)
        .with_callsite(0x5041_4343_414c_4c01)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH | FLAG_METADATA_PROTECTION);

    let ptr = unsafe { alloc.alloc_with_metadata(layout, metadata) };
    if ptr.is_null() {
        println!(
            "{{\"source\":\"pac_metadata_probe\",\"passed\":false,\"error\":\"alloc_with_metadata returned null\"}}"
        );
        std::process::exit(2);
    }
    let pac_probe = pac_probe_snapshot(ptr as usize);
    unsafe {
        ptr.write(0xA5);
        ptr.add(layout.size() - 1).write(0x5A);
        alloc.dealloc_with_metadata(ptr, layout, metadata);
    }

    let reused = unsafe { alloc.alloc_with_metadata(layout, metadata) };
    let reused_same_ptr = reused == ptr;
    if !reused.is_null() {
        unsafe {
            reused.write(0xC3);
            reused.add(layout.size() - 1).write(0x3C);
            alloc.dealloc_with_metadata(reused, layout, metadata);
        }
    }

    let semantic = semantic_stats_snapshot();
    semantic_stats_recording_disable();

    let pac_probe_uses_allocated_object = true;
    let hardware_backend_observed =
        semantic.metadata_pac_auth_signs > 0 || semantic.metadata_pac_auth_verifications > 0;
    let software_fallback_backend_observed = semantic.metadata_pac_software_fallback_signs > 0
        || semantic.metadata_pac_software_fallback_verifications > 0;
    let software_fallback_active = software_fallback_backend_observed && !hardware_backend_observed;
    let backend_observation_consistent = (hardware_backend_observed
        && pac_probe.context_binding_active)
        || (software_fallback_backend_observed && !pac_probe.context_binding_active);
    let hardware_pac_validated = pac_probe.context_binding_active
        && !software_fallback_active
        && pac_probe.strip_roundtrip
        && pac_probe.correct_context_roundtrip
        && pac_probe.wrong_context_rejected
        && pac_probe.active_key != "none"
        && semantic.metadata_pac_auth_signs > 0
        && semantic.metadata_pac_auth_verifications > 0
        && semantic.metadata_pac_auth_failures == 0
        && semantic.metadata_pac_software_fallback_signs == 0
        && semantic.metadata_pac_software_fallback_verifications == 0
        && backend_observation_consistent;
    let software_fallback_validated = software_fallback_active
        && semantic.metadata_pac_auth_signs == 0
        && semantic.metadata_pac_auth_verifications == 0
        && semantic.metadata_pac_auth_failures == 0
        && semantic.metadata_pac_software_fallback_signs > 0
        && semantic.metadata_pac_software_fallback_verifications > 0
        && semantic.metadata_pac_software_fallback_failures == 0
        && backend_observation_consistent;
    let allocator_validated = !ptr.is_null()
        && !reused.is_null()
        && reused_same_ptr
        && semantic.typed_cache_inserts > 0
        && semantic.typed_cache_hits > 0;
    let passed = allocator_validated && (hardware_pac_validated || software_fallback_validated);
    let claim_grade = passed && hardware_pac_validated;

    println!(
        concat!(
            "{{",
            "\"source\":\"pac_metadata_probe\",",
            "\"passed\":{},",
            "\"claim_grade\":{},",
            "\"allocator_validated\":{},",
            "\"hardware_pac_validated\":{},",
            "\"software_fallback_validated\":{},",
            "\"context_binding_active\":{},",
            "\"software_fallback_active\":{},",
            "\"pac_probe_uses_allocated_object\":{},",
            "\"backend_observation_consistent\":{},",
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
            "\"reused_same_ptr\":{},",
            "\"typed_cache_inserts\":{},",
            "\"typed_cache_hits\":{},",
            "\"pac_signs\":{},",
            "\"pac_verifications\":{},",
            "\"pac_failures\":{},",
            "\"pac_software_fallback_signs\":{},",
            "\"pac_software_fallback_verifications\":{},",
            "\"pac_software_fallback_failures\":{}",
            "}}"
        ),
        json_bool(passed),
        json_bool(claim_grade),
        json_bool(allocator_validated),
        json_bool(hardware_pac_validated),
        json_bool(software_fallback_validated),
        json_bool(pac_probe.context_binding_active),
        json_bool(software_fallback_active),
        json_bool(pac_probe_uses_allocated_object),
        json_bool(backend_observation_consistent),
        pac_probe.key,
        pac_probe.active_key,
        pac_probe.matrix_keys[0],
        json_bool(pac_probe.matrix_available[0]),
        json_bool(pac_probe.matrix_signed_changed[0]),
        json_bool(pac_probe.matrix_strip_roundtrip[0]),
        json_bool(pac_probe.matrix_correct_context_roundtrip[0]),
        json_bool(pac_probe.matrix_wrong_context_rejected[0]),
        pac_probe.matrix_keys[1],
        json_bool(pac_probe.matrix_available[1]),
        json_bool(pac_probe.matrix_signed_changed[1]),
        json_bool(pac_probe.matrix_strip_roundtrip[1]),
        json_bool(pac_probe.matrix_correct_context_roundtrip[1]),
        json_bool(pac_probe.matrix_wrong_context_rejected[1]),
        pac_probe.matrix_keys[2],
        json_bool(pac_probe.matrix_available[2]),
        json_bool(pac_probe.matrix_signed_changed[2]),
        json_bool(pac_probe.matrix_strip_roundtrip[2]),
        json_bool(pac_probe.matrix_correct_context_roundtrip[2]),
        json_bool(pac_probe.matrix_wrong_context_rejected[2]),
        pac_probe.matrix_keys[3],
        json_bool(pac_probe.matrix_available[3]),
        json_bool(pac_probe.matrix_signed_changed[3]),
        json_bool(pac_probe.matrix_strip_roundtrip[3]),
        json_bool(pac_probe.matrix_correct_context_roundtrip[3]),
        json_bool(pac_probe.matrix_wrong_context_rejected[3]),
        json_bool(pac_probe.signed_changed),
        json_bool(pac_probe.strip_roundtrip),
        json_bool(pac_probe.correct_context_roundtrip),
        json_bool(pac_probe.wrong_context_rejected),
        json_bool(reused_same_ptr),
        semantic.typed_cache_inserts,
        semantic.typed_cache_hits,
        semantic.metadata_pac_auth_signs,
        semantic.metadata_pac_auth_verifications,
        semantic.metadata_pac_auth_failures,
        semantic.metadata_pac_software_fallback_signs,
        semantic.metadata_pac_software_fallback_verifications,
        semantic.metadata_pac_software_fallback_failures,
    );

    if passed {
        std::process::exit(0);
    }
    std::process::exit(1);
}
