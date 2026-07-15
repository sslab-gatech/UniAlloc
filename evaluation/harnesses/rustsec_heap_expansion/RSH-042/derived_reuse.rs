//! Derived A -> free -> B adapter for RUSTSEC-2026-0128.
//!
//! The published `emap::Keys::next` bug moves an in-place heap owner with
//! `ptr::read`, freeing its buffer while a stale representation remains in the
//! map. This adapter assigns the 64-byte victim buffer one exact manual
//! identity, repeats that identity around the buggy reclaim, then immediately
//! requests an equal-layout `Replacement` with a distinct compiler identity.
//! Address equality is the bounded reuse-edge oracle; the stale owner is never
//! dereferenced by this derived witness.

use emap::{Keys, Map};
use std::hint::black_box;

const VICTIM_TYPE_ID: u64 = 0x5253_482A_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_482A_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_482A_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_482A_0000_0004;
const PAYLOAD_SIZE: usize = 64;

// These hooks are no-ops in the standalone pinned witness. The allocator
// experiment wrapper supplies the explicitly labeled semantic identity scope
// and the immediate denial report. This preserves a normal system-allocator
// address-reuse baseline from the same source file.
pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

#[derive(Clone)]
struct Victim<T>(Vec<T>);

struct Replacement([u8; PAYLOAD_SIZE]);

// Keep the allocation helper noinline so the compiler audit sees a distinct
// critical site with the concrete owner type used by this adapter.
#[inline(never)]
fn materialize_victim(seed: &[u8; PAYLOAD_SIZE]) -> Victim<u8> {
    Victim(seed.to_vec())
}

#[inline(never)]
fn reclaim_next<V>(keys: &mut Keys<V>) -> Option<usize> {
    keys.next()
}

fn main() {
    let seed = [0x41u8; PAYLOAD_SIZE];
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Victim<u8> = materialize_victim;
    let victim = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    assert_eq!(victim.0.len(), PAYLOAD_SIZE);
    assert_eq!(victim.0.capacity(), PAYLOAD_SIZE);

    let mut map: Map<Victim<u8>> = Map::with_capacity_none(1);
    map.insert(0, victim);
    let original_address = map.get(0).expect("victim must exist").0.as_ptr() as usize;

    let mut keys = map.keys();
    let reclaim: fn(&mut Keys<Victim<u8>>) -> Option<usize> = reclaim_next;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || assert_eq!(black_box(reclaim)(&mut keys), Some(0)),
    );
    drop(keys);

    let replacement = Box::new(Replacement([0x42; PAYLOAD_SIZE]));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(&replacement.0);

    drop(replacement);
    drop(map);
}
