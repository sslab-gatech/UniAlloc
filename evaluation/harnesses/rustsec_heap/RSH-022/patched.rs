//! Upstream array-queue 0.4.0 control for RSH-022.
//!
//! The repaired API takes ownership of the pushed value and stores it through
//! `MaybeUninit`; `push_front` no longer invokes user `Clone` after publishing
//! queue state. A panicking `Clone` implementation therefore remains dormant.

use array_queue::ArrayQueue;

struct CloneBomb(String);

impl Clone for CloneBomb {
    fn clone(&self) -> Self {
        panic!("the repaired push_front path must not clone user data")
    }
}

fn main() {
    let mut queue: ArrayQueue<CloneBomb, 2> = ArrayQueue::new();
    queue
        .push_back(CloneBomb(String::from("existing")))
        .expect("first insertion must fit");
    queue
        .push_front(CloneBomb(String::from("front")))
        .expect("second insertion must fit");
    assert_eq!(queue.len(), 2);
}
