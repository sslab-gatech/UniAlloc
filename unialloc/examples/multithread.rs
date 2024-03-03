extern crate alloc;

use alloc::vec::Vec;
use std::thread;

use unialloc::UniAlloc;
#[global_allocator]
static A: UniAlloc = UniAlloc;

fn main() {
    println!("[+] hello from main");

    let th1 = thread::spawn(move || {
        println!("[+] hello from thread1");
        let a = Box::new(8); // allocates memory via our custom allocator crate
        let b = Box::new([0 as u64; 512]);
        println!("{} {}", a, b.len());
        println!("thread 1 end");
    });

    let th2 = thread::spawn(move || {
        println!("[+] hello from thread2");
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
        println!("thread 2 end");
    });

    th2.join().unwrap();
    th1.join().unwrap();
    println!("main end");
}
