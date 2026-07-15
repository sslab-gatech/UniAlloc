//! RUSTSEC-2020-0047 / array-queue.
//!
//! Heap-owning payload adapter for Rudra-PoC 0017 and upstream issue 2. After
//! `start` advances, vulnerable array-queue 0.4.0 indexes `pop_back` by logical
//! length alone. The final pop therefore reconstructs the already-freed front
//! Box from stale `MaybeUninit` bytes. Version 0.4.1 contains upstream PR 7's
//! index fix and runs the same schedule cleanly.

use array_queue::ArrayQueue;

fn main() {
    let mut queue: ArrayQueue<Box<u64>, 3> = ArrayQueue::new();
    queue.push_back(Box::new(0x41)).unwrap();
    queue.push_back(Box::new(0x42)).unwrap();
    queue.push_back(Box::new(0x43)).unwrap();

    drop(queue.pop_front().unwrap());
    drop(queue.pop_back().unwrap());
    drop(queue.pop_back().unwrap());
}
