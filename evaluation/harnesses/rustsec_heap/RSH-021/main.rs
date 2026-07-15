//! RUSTSEC-2026-0143 / oneringbuf 0.7.0 with the `vmem` feature.
//!
//! Vmem storage bit-copies the input buffer, drops the source elements, and
//! later drops the copied elements.  The duplicated inner Vec pointers give a
//! deterministic ASan double-free and normally a glibc tcache abort.

fn main() {
    let values: Vec<Vec<u32>> = (0..1024).map(|i| vec![i, i + 1, i + 2]).collect();
    let ring: oneringbuf::SharedVmemRB<Vec<u32>> = oneringbuf::SharedVmemRB::from(values);
    drop(ring);
}
