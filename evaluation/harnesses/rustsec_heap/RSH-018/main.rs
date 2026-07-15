//! RUSTSEC-2025-0053 / arenavec 0.1.1.
//!
//! `SliceVec::split_off(0)` bit-copies owned elements and leaves the original
//! length unchanged, so both vectors drop the same `String` allocation.

use arenavec::rc::{Arena, SliceVec};
use arenavec::ArenaBacking;

#[derive(Debug)]
struct Tracked(String);

impl Drop for Tracked {
    fn drop(&mut self) {
        println!("freeing={:?}", self.0.as_ptr());
    }
}

fn main() {
    let arena =
        Arena::init_capacity(ArenaBacking::MemoryMap, 512).expect("arena allocation must succeed");
    let mut left: SliceVec<Tracked> = SliceVec::with_capacity(arena.inner(), 4);
    left.push(Tracked(String::from("BUG")));
    left.push(Tracked(String::from("BUG")));
    let _right = left.split_off(0);
}
