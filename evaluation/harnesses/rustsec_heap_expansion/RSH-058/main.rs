//! Pinned Rudra-PoC 0108 for RUSTSEC-2021-0010.
//!
//! Origin commit: 6226dd030fffbed5601099cb0e24f73e4150a7f5. The published
//! witness already uses `Box<u64>`, so its duplicate reclaim is directly
//! allocator-visible without payload changes.

#![forbid(unsafe_code)]

use containers::collections::b_tree::BTree;
use default_allocator::Heap;
use rel::Core;

fn main() {
    if let Some(mut btree) = BTree::<i32, Box<u64>, Core, Heap>::new(Core, 20) {
        if btree.insert(2, Box::new(1)).is_ok() {
            // The published loop cannot begin a second iteration because this
            // closure always panics. Keep its first (vulnerable) call as a
            // bounded harness so aborting patched controls cannot be retried.
            let _ = btree.insert_with(2, |value| {
                let retained = match value {
                    Some(value) => value,
                    None => Box::new(0),
                };
                None::<Option<u64>>.unwrap();
                retained
            });
        }
    }
}
