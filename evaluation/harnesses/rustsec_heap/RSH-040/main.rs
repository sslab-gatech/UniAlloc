//! RUSTSEC-2025-0105 / direct_ring_buffer 0.2.1.
//!
//! Safe construction creates typed `bool` storage with `Vec::set_len` before
//! initialization.  `write_slices` then materializes a mutable typed slice over
//! those invalid values.  Miri is the primary oracle.

use direct_ring_buffer::create_ring_buffer;
fn main() {
    let (mut producer, _consumer) = create_ring_buffer::<bool>(10);
    producer.write_slices(|_slice, _| 0, None);
}
