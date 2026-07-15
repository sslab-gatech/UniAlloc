//! RUSTSEC-2025-0049 / scratchpad 1.3.1.
//!
//! Safe implementations of `Tracking` and `Buffer` steer safe scratchpad code
//! to a forged offset. ASan reports a 16-byte heap-buffer-overflow write.

#![forbid(unsafe_code)]

use scratchpad::*;

struct Storage(Vec<u8>);
struct ForgedTracking(String);

impl Tracking for ForgedTracking {
    fn set(&mut self, _: usize, _: usize) {}

    fn capacity(&self) -> usize {
        12
    }

    fn get(&self, _: usize) -> usize {
        256
    }
}

impl Buffer for Storage {
    fn as_bytes_mut(&mut self) -> &mut [u8] {
        &mut self.0[..]
    }

    fn as_bytes(&self) -> &[u8] {
        &self.0[..]
    }
}

fn main() {
    let storage = Storage(vec![111, 110, 108, 47, 115, 104, 97, 114, 101, 47, 97]);
    let tracking = ForgedTracking(String::from("BUG"));
    std::hint::black_box(&tracking.0);
    let scratchpad = Scratchpad::new(storage, tracking);
    let marker = scratchpad.mark_back().expect("marker allocation");
    let source: [u8; 16] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15];
    let allocation = marker.allocate_slice_copy::<[u8], _>(&source[..]);
    std::hint::black_box(allocation);
}
