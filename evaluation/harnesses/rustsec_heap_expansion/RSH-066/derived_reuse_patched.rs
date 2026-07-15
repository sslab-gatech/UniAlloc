//! Safe same-identity control for the derived RUSTSEC-2024-0007 reuse edge.
//!
//! rust-i18n-support 3.0.1 returns a guard that keeps the selected string
//! alive. This control releases the guard, reclaims the same 4096-byte owned
//! string through a normal `AtomicStr` drop, and allocates a second
//! `AtomicStr` under the exact same manual identity. It retains only the first
//! address as an integer and controls for ordinary same-identity reuse.

#![forbid(unsafe_code)]

use rust_i18n_support::AtomicStr;
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_4842_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4842_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4842_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4842_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4842_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4842_0000_0006;
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

#[inline(never)]
fn materialize_atomic(value: &str) -> AtomicStr {
    AtomicStr::new(value)
}

#[inline(never)]
fn reclaim_value<T>(value: T) {
    drop(value);
}

fn main() {
    let victim_seed = "A".repeat(PAYLOAD_SIZE);
    // Allocate both immutable inputs before the treatment lifecycle so the
    // replacement seed cannot consume the just-reclaimed victim buffer.
    let replacement_seed = "B".repeat(PAYLOAD_SIZE);
    let materialize: fn(&str) -> AtomicStr = materialize_atomic;
    let victim = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&victim_seed),
    );
    let guard = victim.as_str();
    let original_address = guard.as_ptr() as usize;
    drop(guard);
    let reclaim: fn(AtomicStr) = reclaim_value;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(victim),
    );

    let replacement = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || black_box(materialize)(&replacement_seed),
    );
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_guard = replacement.as_str();
    let replacement_address = replacement_guard.as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement_guard);
    drop(replacement_guard);

    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || black_box(reclaim)(replacement),
    );
}
