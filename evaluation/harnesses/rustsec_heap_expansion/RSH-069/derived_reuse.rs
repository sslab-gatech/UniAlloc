//! Layout-bounded reduction of the freed-properties edge in RUSTSEC-2025-0022.
//!
//! The vulnerable OpenSSL wrapper consumed an `Option<CString>` while deriving
//! the pointer passed to FFI, so the CString allocation was reclaimed before
//! OpenSSL read it. This adapter preserves that consume -> free -> stale C-string
//! read sequence and inserts a synthetic 4096-byte foreign replacement.

use std::ffi::{c_char, CStr, CString};
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4869_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4869_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4869_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4869_0000_0004;
const PAYLOAD_SIZE: usize = 4096;

pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

#[repr(align(1))]
struct Replacement([u8; PAYLOAD_SIZE]);

#[inline(never)]
fn materialize_bytes<T: Clone, const N: usize>(seed: &[T; N]) -> Vec<T> {
    seed.to_vec()
}

#[inline(never)]
fn consume_owner<T, R: Copy>(owner: Option<T>, default: R, project: fn(&T) -> R) -> R {
    owner.map_or(default, |value| {
        let result = project(&value);
        drop(value);
        result
    })
}

fn cstring_pointer(value: &CString) -> *const c_char {
    value.as_ptr()
}

fn main() {
    let mut seed = [b'A'; PAYLOAD_SIZE];
    seed[PAYLOAD_SIZE - 1] = 0;
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Vec<u8> = materialize_bytes;
    let bytes = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    assert_eq!(bytes.capacity(), PAYLOAD_SIZE);
    let original_address = bytes.as_ptr() as usize;
    let properties = unsafe { CString::from_vec_with_nul_unchecked(bytes) };
    assert_eq!(properties.as_ptr() as usize, original_address);

    // Mirrors the vulnerable map_or lifetime: the raw pointer escapes while
    // the generic owner-consumption helper reclaims the CString under the
    // manually attributed victim identity.
    let consume: fn(
        Option<CString>,
        *const c_char,
        fn(&CString) -> *const c_char,
    ) -> *const c_char = consume_owner;
    let stale = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(consume)(Some(properties), std::ptr::null(), cstring_pointer),
    );

    let mut replacement = Box::new(Replacement([b'B'; PAYLOAD_SIZE]));
    replacement.0[PAYLOAD_SIZE - 1] = 0;
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");

    if original_address == replacement_address {
        let observed = unsafe { CStr::from_ptr(stale) };
        assert_eq!(observed.to_bytes()[0], b'B');
        black_box(observed);
    }
    black_box(&replacement.0);
}
