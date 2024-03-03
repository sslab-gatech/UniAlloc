extern crate alloc;

use unialloc::UniAlloc;
#[global_allocator]
static A: UniAlloc = UniAlloc;

use alloc::vec::Vec;
use rand::prelude::*;

fn main() {
    {
        let a = Box::new(8); // allocates memory via our custom allocator crate
        let b = Box::new([0 as u64; 512]);
        println!("{} {}", a, b.len());
    }

    println!("hello2");

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

    for x in &vec {
        println!("{}", x);
    }
    assert_eq!(vec, [7, 1, 2, 3]);

    let num = 5000;
    let mut v = Vec::<usize>::with_capacity(num);

    for _ in 0..num {
        v.push(0);
    }

    for _ in 0..num {
        v.pop();
    }

    let r: u32 = rand::thread_rng().gen();
    let x: usize = (r % 4096) as usize;

    let mut v2 = Vec::<usize>::with_capacity(x);

    for _ in 0..x {
        v2.push(0);
    }

    for _ in 0..x {
        v2.pop();
        // println!("len: {}", v2.len());
    }
}
