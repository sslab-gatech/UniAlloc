extern crate test;
use supper::TypedArena;
use core::cell::Cell;
use test::Bencher;


#[test]
pub fn test_copy() {
    let arena = TypedArena::default();
    for _ in 0..100000 {
        arena.alloc(Point { x: 1, y: 2, z: 3 });
    }
}