//! RUSTSEC-2018-0009 / crossbeam 0.4.0.
//!
//! Minimal reduction of the MsQueue failure diagnosed in upstream issue 82
//! and fixed by crossbeam PR 184. `try_pop` moves a payload out with
//! `ptr::read`, while vulnerable retired nodes still drop that payload during
//! epoch reclamation. The tag makes the queue's pre-existing uninitialized
//! sentinel inert so the witness isolates the reported moved-payload double
//! drop. Repeated queue operations deterministically advance the same default
//! epoch collector used by the upstream failure trace.

use crossbeam::queue::MsQueue;

const LIVE: usize = 0x51f0_47aa_bbad_f00d;

struct HeapOwner {
    ptr: *mut u64,
    tag: usize,
}

impl HeapOwner {
    fn new(value: u64) -> Self {
        Self {
            ptr: Box::into_raw(Box::new(value)),
            tag: LIVE,
        }
    }
}

impl Drop for HeapOwner {
    fn drop(&mut self) {
        if self.tag == LIVE {
            // SAFETY: an initialized HeapOwner is created only by `new` and
            // owns exactly this Box. The advisory bug creates the duplicate
            // destructor invocation that AddressSanitizer observes.
            unsafe {
                drop(Box::from_raw(self.ptr));
            }
        }
    }
}

fn main() {
    let queue = MsQueue::new();

    for value in 0..512 {
        queue.push(HeapOwner::new(value));
        drop(queue.try_pop().expect("the value was just pushed"));
    }

    // Retain the last sentinel: only epoch-reclaimed retired nodes participate
    // in the oracle, matching the upstream stack trace.
    std::mem::forget(queue);
}
