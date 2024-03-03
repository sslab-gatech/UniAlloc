extern crate alloc;

use unialloc::UniAlloc;
#[global_allocator]
static A: UniAlloc = UniAlloc;

use alloc::{boxed::Box, vec::Vec};
use std::thread;

use rand::{thread_rng, Rng};

const NTHREADS: u32 = 2000;

fn main() {
    let mut children = vec![];

    for i in 0..NTHREADS {
        // Spin up another thread
        children.push(thread::spawn(move || {
            let mut rng = rand::thread_rng();
            let mut sz: u32 = rng.gen();
            sz = sz % 2048;
            sz += 1;
            let mut m: u32 = rng.gen();

            m = m % sz;

            println!("this is thread number {}, alloc size {}", i, sz);
            let new_len = sz as usize;
            let mut values = vec![0; new_len];
            values.remove(m as usize);
            values.resize(new_len / 3, 3);
        }));
    }

    for child in children {
        // Wait for the thread to finish. Returns a result.
        let _ = child.join();
    }
}
