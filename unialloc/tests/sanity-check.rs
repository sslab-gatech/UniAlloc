extern crate alloc;
use alloc::vec::Vec;
include!("allocator.rs");

#[test]
fn sanity_check() {
    {
        let a = Box::new(8); // allocates memory via our custom allocator crate
        let b = Box::new([0 as u64; 512]);
        assert_eq!(*a, 8);
        assert_eq!(b.len(), 512);
    }

    let mut vec = Vec::new();
    vec.push(1);
    vec.push(2);

    assert_eq!(vec.len(), 2);
    assert_eq!(vec[0], 1);

    assert_eq!(vec.pop(), Some(2));
    assert_eq!(vec.len(), 1);

    vec[0] = 7;
    assert_eq!(vec[0], 7);

    vec.extend([1, 2, 3].iter().copied());

    assert_eq!(vec, [7, 1, 2, 3]);
}
