#![no_std]
#![no_main]
#![feature(alloc_error_handler)]

use core::alloc::Layout;
use core::ffi::c_void;
use core::panic::PanicInfo;

use unialloc::alloc_api::{
    __unialloc_alloc_layout_with_metadata_local, __unialloc_dealloc_layout_with_metadata_local,
    FLAG_METADATA_PROTECTION, FLAG_POINTER_AUTH,
};
use unialloc::{
    metadata_pointer_auth_runtime_probe, semantic_stats_recording_disable, semantic_stats_reset,
    semantic_stats_snapshot, AllocationMetadata, UniAlloc, FLAG_TYPE_ISOLATED,
};

#[global_allocator]
static GLOBAL_ALLOCATOR: UniAlloc = UniAlloc::new();

#[cfg(target_os = "macos")]
#[link(name = "System", kind = "dylib")]
extern "C" {}

extern "C" {
    fn write(fd: i32, buf: *const c_void, count: usize) -> isize;
    fn _exit(status: i32) -> !;
}

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

struct JsonBuf {
    bytes: [u8; 4096],
    len: usize,
}

impl JsonBuf {
    const fn new() -> Self {
        Self {
            bytes: [0; 4096],
            len: 0,
        }
    }

    fn push_byte(&mut self, byte: u8) {
        if self.len < self.bytes.len() {
            self.bytes[self.len] = byte;
            self.len += 1;
        }
    }

    fn push_str(&mut self, text: &str) {
        for byte in text.as_bytes() {
            self.push_byte(*byte);
        }
    }

    fn push_bool(&mut self, value: bool) {
        self.push_str(if value { "true" } else { "false" });
    }

    fn push_usize(&mut self, mut value: usize) {
        if value == 0 {
            self.push_byte(b'0');
            return;
        }
        let mut buf = [0u8; 20];
        let mut idx = buf.len();
        while value != 0 {
            idx -= 1;
            buf[idx] = b'0' + (value % 10) as u8;
            value /= 10;
        }
        for byte in &buf[idx..] {
            self.push_byte(*byte);
        }
    }

    fn push_key_bool(&mut self, key: &str, value: bool) {
        self.push_byte(b'"');
        self.push_str(key);
        self.push_str("\":");
        self.push_bool(value);
    }

    fn push_key_usize(&mut self, key: &str, value: usize) {
        self.push_byte(b'"');
        self.push_str(key);
        self.push_str("\":");
        self.push_usize(value);
    }

    fn push_key_str(&mut self, key: &str, value: &str) {
        self.push_byte(b'"');
        self.push_str(key);
        self.push_str("\":\"");
        self.push_str(value);
        self.push_byte(b'"');
    }
}

fn write_all(fd: i32, bytes: &[u8]) {
    let mut offset = 0;
    while offset < bytes.len() {
        let written = unsafe {
            write(
                fd,
                bytes[offset..].as_ptr().cast::<c_void>(),
                bytes.len() - offset,
            )
        };
        if written <= 0 {
            break;
        }
        offset += written as usize;
    }
}

fn exit_now(status: i32) -> ! {
    unsafe { _exit(status) }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    write_all(
        1,
        b"{\"source\":\"pac_metadata_probe\",\"runtime\":\"nostd\",\"passed\":false,\"error\":\"panic\"}\n",
    );
    exit_now(101)
}

#[alloc_error_handler]
fn alloc_error(_layout: Layout) -> ! {
    write_all(
        1,
        b"{\"source\":\"pac_metadata_probe\",\"runtime\":\"nostd\",\"passed\":false,\"error\":\"alloc_error\"}\n",
    );
    exit_now(102)
}

fn emit_null_alloc_and_exit() -> ! {
    write_all(
        1,
        b"{\"source\":\"pac_metadata_probe\",\"runtime\":\"nostd\",\"passed\":false,\"error\":\"raw alloc returned null\"}\n",
    );
    exit_now(2)
}

#[no_mangle]
pub extern "C" fn main(_argc: i32, _argv: *const *const u8) -> i32 {
    // Exercise the semantic side-cache path, not merely raw allocation.  The
    // arm64e build uses a pthread-key backed thread cache and a small locked
    // inline segregated cache so this no_std probe can validate real
    // insert/hit reuse without Rust TLV startup.
    semantic_stats_reset();

    let layout = Layout::from_size_align(
        4 * core::mem::size_of::<usize>(),
        core::mem::align_of::<usize>(),
    )
    .unwrap_or_else(|_| exit_now(103));
    let metadata = AllocationMetadata::for_type(0x5041_434d_4554_4101)
        .with_module(0x5041_434d_4f44_0001)
        .with_callsite(0x5041_4343_414c_4c01)
        .with_flags(FLAG_TYPE_ISOLATED | FLAG_POINTER_AUTH | FLAG_METADATA_PROTECTION);

    let ptr = unsafe {
        __unialloc_alloc_layout_with_metadata_local(
            layout,
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.callsite,
        )
    };
    if ptr.is_null() {
        emit_null_alloc_and_exit();
    }
    unsafe {
        ptr.write(0xA5);
        ptr.add(layout.size() - 1).write(0x5A);
    }

    let pac_probe = pac_probe_snapshot(ptr as usize);
    let auth_probe = unsafe { metadata_pointer_auth_runtime_probe(ptr, layout, metadata) };

    unsafe {
        __unialloc_dealloc_layout_with_metadata_local(
            ptr,
            layout,
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.callsite,
        );
    }

    let reused = unsafe {
        __unialloc_alloc_layout_with_metadata_local(
            layout,
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.callsite,
        )
    };
    let reused_same_ptr = reused == ptr;
    if reused.is_null() {
        emit_null_alloc_and_exit();
    }
    unsafe {
        reused.write(0xC3);
        reused.add(layout.size() - 1).write(0x3C);
        __unialloc_dealloc_layout_with_metadata_local(
            reused,
            layout,
            metadata.type_id,
            metadata.module_id,
            metadata.flags,
            metadata.callsite,
        );
    }

    let semantic = semantic_stats_snapshot();
    semantic_stats_recording_disable();

    let allocator_validated =
        reused_same_ptr && semantic.typed_cache_inserts > 0 && semantic.typed_cache_hits > 0;
    let hardware_pac_validated = auth_probe.backend_hardware_pac
        && auth_probe.auth_nonzero
        && auth_probe.signed_changed
        && auth_probe.correct_context_valid
        && auth_probe.wrong_layout_rejected
        && auth_probe.wrong_metadata_rejected
        && pac_probe.context_binding_active
        && pac_probe.active_key != "none"
        && semantic.metadata_pac_auth_signs > 0
        && semantic.metadata_pac_auth_verifications > 0
        && semantic.metadata_pac_auth_failures == 0
        && semantic.metadata_pac_software_fallback_signs == 0
        && semantic.metadata_pac_software_fallback_verifications == 0;
    let software_fallback_validated = auth_probe.backend_software_fallback
        && auth_probe.auth_nonzero
        && auth_probe.correct_context_valid
        && auth_probe.wrong_layout_rejected
        && auth_probe.wrong_metadata_rejected
        && semantic.metadata_pac_software_fallback_signs > 0
        && semantic.metadata_pac_software_fallback_verifications > 0
        && semantic.metadata_pac_software_fallback_failures == 0;
    let passed = allocator_validated && (hardware_pac_validated || software_fallback_validated);

    let mut json = JsonBuf::new();
    json.push_byte(b'{');
    json.push_key_str("source", "pac_metadata_probe");
    json.push_byte(b',');
    json.push_key_str("runtime", "nostd");
    json.push_byte(b',');
    json.push_key_bool("passed", passed);
    json.push_byte(b',');
    json.push_key_bool("claim_grade", passed && hardware_pac_validated);
    json.push_byte(b',');
    json.push_key_bool("allocator_validated", allocator_validated);
    json.push_byte(b',');
    json.push_key_bool("hardware_pac_validated", hardware_pac_validated);
    json.push_byte(b',');
    json.push_key_bool("software_fallback_validated", software_fallback_validated);
    json.push_byte(b',');
    json.push_key_bool("context_binding_active", pac_probe.context_binding_active);
    json.push_byte(b',');
    json.push_key_bool(
        "software_fallback_active",
        auth_probe.backend_software_fallback,
    );
    json.push_byte(b',');
    json.push_key_bool("pac_probe_uses_allocated_object", true);
    json.push_byte(b',');
    json.push_key_bool("backend_observation_consistent", passed);
    json.push_byte(b',');
    json.push_key_bool("full_allocator_side_cache_validated", allocator_validated);
    json.push_byte(b',');
    json.push_key_bool(
        "metadata_pac_negative_auth_requires_signal_probe",
        cfg!(unialloc_target_arm64e),
    );
    json.push_byte(b',');
    json.push_key_str("pac_probe_key", pac_probe.key);
    json.push_byte(b',');
    json.push_key_str("pac_probe_active_key", pac_probe.active_key);
    json.push_str(",\"pac_probe_key_matrix\":[");
    for idx in 0..4 {
        if idx != 0 {
            json.push_byte(b',');
        }
        json.push_byte(b'{');
        json.push_key_str("key", pac_probe.matrix_keys[idx]);
        json.push_byte(b',');
        json.push_key_bool("available", pac_probe.matrix_available[idx]);
        json.push_byte(b',');
        json.push_key_bool("signed_changed", pac_probe.matrix_signed_changed[idx]);
        json.push_byte(b',');
        json.push_key_bool("strip_roundtrip", pac_probe.matrix_strip_roundtrip[idx]);
        json.push_byte(b',');
        json.push_key_bool(
            "correct_context_roundtrip",
            pac_probe.matrix_correct_context_roundtrip[idx],
        );
        json.push_byte(b',');
        json.push_key_bool(
            "wrong_context_rejected",
            pac_probe.matrix_wrong_context_rejected[idx],
        );
        json.push_byte(b'}');
    }
    json.push_byte(b']');
    json.push_byte(b',');
    json.push_key_bool("pac_probe_signed_changed", pac_probe.signed_changed);
    json.push_byte(b',');
    json.push_key_bool("pac_probe_strip_roundtrip", pac_probe.strip_roundtrip);
    json.push_byte(b',');
    json.push_key_bool(
        "pac_probe_correct_context_roundtrip",
        pac_probe.correct_context_roundtrip,
    );
    json.push_byte(b',');
    json.push_key_bool(
        "pac_probe_wrong_context_rejected",
        pac_probe.wrong_context_rejected,
    );
    json.push_byte(b',');
    json.push_key_bool(
        "metadata_pac_wrong_layout_rejected",
        auth_probe.wrong_layout_rejected,
    );
    json.push_byte(b',');
    json.push_key_bool(
        "metadata_pac_wrong_metadata_rejected",
        auth_probe.wrong_metadata_rejected,
    );
    json.push_byte(b',');
    json.push_key_bool("reused_same_ptr", reused_same_ptr);
    json.push_byte(b',');
    json.push_key_usize("typed_cache_inserts", semantic.typed_cache_inserts);
    json.push_byte(b',');
    json.push_key_usize("typed_cache_hits", semantic.typed_cache_hits);
    json.push_byte(b',');
    json.push_key_usize("pac_signs", semantic.metadata_pac_auth_signs);
    json.push_byte(b',');
    json.push_key_usize(
        "pac_verifications",
        semantic.metadata_pac_auth_verifications,
    );
    json.push_byte(b',');
    json.push_key_usize("pac_failures", semantic.metadata_pac_auth_failures);
    json.push_byte(b',');
    json.push_key_usize(
        "pac_software_fallback_signs",
        semantic.metadata_pac_software_fallback_signs,
    );
    json.push_byte(b',');
    json.push_key_usize(
        "pac_software_fallback_verifications",
        semantic.metadata_pac_software_fallback_verifications,
    );
    json.push_byte(b',');
    json.push_key_usize(
        "pac_software_fallback_failures",
        semantic.metadata_pac_software_fallback_failures,
    );
    json.push_byte(b'}');
    json.push_byte(b'\n');
    write_all(1, &json.bytes[..json.len]);

    if passed {
        0
    } else {
        1
    }
}
