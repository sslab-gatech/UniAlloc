//! Derived cross-identity reuse edge for RUSTSEC-2022-0028.
//!
//! The published Neon defect exposes a Rust `Vec<u8>` backing store to
//! JavaScript and then releases its owner. This bounded adapter preserves that
//! ownership sequence with an `ExternalView`, immediately requests an
//! equal-layout allocation of a distinct Rust type, and reads through the
//! external view only when the two addresses collide. Address equality is the
//! treatment oracle; the claim scope ends at this bounded reuse decision.

use std::hint::black_box;
use std::mem::{align_of, size_of};

const VICTIM_TYPE_ID: u64 = 0x5253_4840_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4840_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4840_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4840_0000_0004;
const PAYLOAD_SIZE: usize = 4;

// These hooks are no-ops in standalone execution. The allocator experiment
// wrapper supplies the exact manual victim identity and the bound denial
// report for typed_plain and typeiso arms.
pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

#[derive(Clone, Copy)]
struct ExternalView {
    pointer: *const u8,
    length: usize,
}

impl ExternalView {
    unsafe fn read_after_owner_release(self) -> u8 {
        assert_eq!(self.length, PAYLOAD_SIZE);
        unsafe { std::ptr::read_volatile(self.pointer) }
    }
}

#[repr(transparent)]
struct Replacement([u8; PAYLOAD_SIZE]);

// Keep the allocation helper noinline so the compiler audit sees a distinct
// critical site with the concrete owner type used by this adapter.
#[inline(never)]
fn materialize_victim(seed: &[u8; PAYLOAD_SIZE]) -> Vec<u8> {
    seed.to_vec()
}

#[inline(never)]
fn reclaim_victim<T>(victim: T) {
    drop(victim);
}

fn main() {
    assert_eq!(size_of::<Replacement>(), PAYLOAD_SIZE);
    assert_eq!(align_of::<Replacement>(), align_of::<u8>());

    let seed = [0u8, 1, 2, 3];
    let materialize: fn(&[u8; PAYLOAD_SIZE]) -> Vec<u8> = materialize_victim;
    let victim = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    assert_eq!(victim.len(), PAYLOAD_SIZE);
    assert_eq!(victim.capacity(), PAYLOAD_SIZE);
    let external = ExternalView {
        pointer: victim.as_ptr(),
        length: victim.len(),
    };
    let original_address = external.pointer as usize;

    let reclaim: fn(Vec<u8>) = reclaim_victim;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(victim),
    );

    let replacement = Box::new(Replacement([0x64; PAYLOAD_SIZE]));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;

    let stale_read = if original_address == replacement_address {
        Some(unsafe { external.read_after_owner_release() })
    } else {
        None
    };
    eprintln!(
        "original={original_address:#x} replacement={replacement_address:#x} stale_read={stale_read:?}"
    );
    if let Some(observed) = stale_read {
        assert_eq!(observed, replacement.0[0]);
    }
    black_box(&replacement.0);
}
