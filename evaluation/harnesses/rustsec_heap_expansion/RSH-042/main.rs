//! RUSTSEC-2026-0128 / emap 0.0.13.
//!
//! This is a mechanical adapter of the safe-API reproducer published in
//! upstream issue #168. `Keys::next` moves an in-place `Option<String>` with
//! `ptr::read`, so its temporary frees the String while the map retains the
//! dangling representation. Printing the subsequent `Map::get` reference gives
//! AddressSanitizer a deterministic heap-use-after-free oracle.

use emap::Map;

fn main() {
    let mut map: Map<String> = Map::with_capacity_none(1);
    map.insert(0, String::from("hello"));

    let mut keys = map.keys();
    assert_eq!(keys.next(), Some(0));

    let dangling = map.get(0).expect("the vulnerable slot remains Some");
    println!("{dangling}");
}
