//! Derived A -> free -> B adapter for RUSTSEC-2019-0023.
//!
//! string-interner 0.7.0 derives `Clone` for an interner that contains raw
//! string references. The cloned map therefore keeps references into `old`
//! even though its `values` field owns separately cloned `Box<str>` values.
//! Dropping `old` reclaims the 64-byte string while `new` retains the stale
//! map entry. This bounded adapter immediately requests an equal-layout,
//! semantically distinct `Replacement`; address equality is the reuse-edge
//! oracle. It deliberately does not claim to detect the original stale read.

use std::hint::black_box;
use string_interner::{DefaultStringInterner, Sym};

const VICTIM_TYPE_ID: u64 = 0x5253_4834_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4834_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4834_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4834_0000_0004;
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

struct Replacement([u8; PAYLOAD_SIZE]);

#[inline(never)]
fn materialize_old() -> (DefaultStringInterner, Sym, usize) {
    // Capacity is reserved outside the identity scope so the annotated
    // allocation is the vulnerable Box<str> payload, rather than a Vec or
    // HashMap growth allocation with a different layout.
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

    // The vulnerable clone's map now points into the reclaimed old payload.
    // Keep it alive to preserve the source-level stale-reference state while
    // the derived experiment isolates the following A-to-B reuse edge.
    black_box(&new);
    black_box(symbol);

    let replacement = Box::new(Replacement([0x42; PAYLOAD_SIZE]));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement.0);

    drop(replacement);
    drop(new);
}
