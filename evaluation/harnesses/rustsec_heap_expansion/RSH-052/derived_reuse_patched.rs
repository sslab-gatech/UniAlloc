//! Safe same-identity control for the derived RUSTSEC-2019-0023 reuse edge.
//!
//! string-interner 0.7.1 rebuilds the cloned map so `new` references its own
//! cloned strings. This control allocates and reclaims the same 64-byte
//! `Box<str>` victim, validates that the clone remains sound, and requests a
//! second 64-byte `Box<str>` under the exact same identity. It controls for
//! ordinary same-identity cache reuse without retaining a stale pointer.

use std::hint::black_box;
use string_interner::{DefaultStringInterner, Sym};

const VICTIM_TYPE_ID: u64 = 0x5253_4834_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4834_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4834_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4834_0000_0004;
const REPLACEMENT_ALLOC_CALLSITE: u64 = 0x5253_4834_0000_0005;
const REPLACEMENT_RECLAIM_CALLSITE: u64 = 0x5253_4834_0000_0006;
const PAYLOAD_SIZE: usize = 64;
const PAYLOAD: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

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
fn materialize_old() -> (DefaultStringInterner, Sym, usize) {
    let mut old = DefaultStringInterner::with_capacity(1);
    let symbol = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || old.get_or_intern(PAYLOAD),
    );
    let address = old
        .resolve(symbol)
        .expect("the victim string must be interned")
        .as_ptr() as usize;
    (old, symbol, address)
}

fn main() {
    assert_eq!(PAYLOAD.len(), PAYLOAD_SIZE);

    let (old, symbol, original_address) = materialize_old();
    let new = old.clone();
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || drop(old),
    );

    assert_eq!(new.resolve(symbol), Some(PAYLOAD));

    // Reserve the replacement interner's collection storage outside the
    // identity scope, then use the same dependency allocation path and exact
    // identity as the victim. This keeps the control free from an automatic
    // Box allocation identity that would turn it into another A-to-B edge.
    let mut replacement = DefaultStringInterner::with_capacity(1);
    let replacement_symbol = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_ALLOC_CALLSITE,
        || replacement.get_or_intern(PAYLOAD),
    );
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = replacement
        .resolve(replacement_symbol)
        .expect("the replacement string must be interned")
        .as_ptr() as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement);

    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        REPLACEMENT_RECLAIM_CALLSITE,
        || drop(replacement),
    );
    drop(new);
}
