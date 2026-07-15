//! RUSTSEC-2025-0053 / arenavec issue 5: capacity accounting permits writes
//! past a fixed arena allocation.

#![forbid(unsafe_code)]

#[derive(PartialEq, PartialOrd)]
struct Payload(String);

fn main() {
    let arena =
        arenavec::rc::Arena::init_capacity(arenavec::common::ArenaBacking::SystemAllocation, 136)
            .expect("arena allocation must succeed");
    let mut values = arenavec::common::SliceVec::with_capacity(arena.inner(), 2);
    for _ in 0..4 {
        values.push(Payload(String::from("BUG")));
    }
}
