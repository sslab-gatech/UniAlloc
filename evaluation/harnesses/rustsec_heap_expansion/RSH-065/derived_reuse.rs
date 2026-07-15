//! Layout-bounded reduction of the stale-pointer edge in RUSTSEC-2023-0054.
//!
//! The vulnerable `vec_insert_bytes` computes an insertion pointer before a
//! reserve can relocate and reclaim the old Vec buffer, then copies through
//! that stale pointer. This adapter models the reserve relocation explicitly:
//! it allocates/copies the grown buffer while the victim is live, reclaims the
//! old buffer, inserts an equal-layout foreign replacement, and performs the
//! stale copy only after measured address reuse.

use std::hint::black_box;
use std::ptr;

const VICTIM_TYPE_ID: u64 = 0x5253_4865_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4865_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4865_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4865_0000_0004;
const INITIAL_CAPACITY: usize = 4;
const INSERTED_LEN: usize = 4096;

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
struct Replacement([u8; INITIAL_CAPACITY]);

// Keep the allocation helper noinline so the compiler audit sees a distinct
// critical site with the concrete owner type used by this adapter.
#[inline(never)]
fn materialize_target(seed: &[u8; INITIAL_CAPACITY]) -> Vec<u8> {
    seed.to_vec()
}

#[inline(never)]
fn reclaim_target<T>(value: T) {
    drop(value);
}

fn main() {
    let seed = *b"abcd";
    let materialize: fn(&[u8; INITIAL_CAPACITY]) -> Vec<u8> = materialize_target;
    let target = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || black_box(materialize)(&seed),
    );
    assert_eq!(target.capacity(), INITIAL_CAPACITY);
    let insertion_point = 2;
    let stale = unsafe { target.as_ptr().add(insertion_point) };
    let original_address = target.as_ptr() as usize;

    // Model the reserve's new-buffer allocation and live copy before the old
    // victim is reclaimed. The new allocation has a different layout.
    let mut grown = Vec::with_capacity(INITIAL_CAPACITY + INSERTED_LEN);
    grown.extend_from_slice(&target);
    let reclaim: fn(Vec<u8>) = reclaim_target;
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || black_box(reclaim)(target),
    );

    let replacement = Box::new(Replacement(*b"WXYZ"));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&*replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");

    if original_address == replacement_address {
        unsafe {
            let old_len = grown.len();
            ptr::copy(
                stale,
                grown.as_mut_ptr().add(insertion_point + 1),
                old_len - insertion_point,
            );
            grown.as_mut_ptr().add(insertion_point).write(b'X');
            grown.set_len(old_len + 1);
        }
    }

    black_box(&replacement.0);
    black_box(&grown);
}
