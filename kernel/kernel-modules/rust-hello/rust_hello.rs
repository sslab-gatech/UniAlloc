#![no_std]
#![feature(allocator_api, global_asm)]
#![feature(asm)]

use kernel::prelude::*;
use alloc::boxed::Box;
use alloc::vec::Vec;
use kernel::bencher::bench_it;


module! {
    type: RustMinimal,
    name: b"rust_minimal",
    author: b"Rust for Linux Contributors",
    description: b"Rust minimal sample",
    license: b"GPL v2",
    params: {
    },
}

struct RustMinimal {
    message: String,
}

impl KernelModule for RustMinimal {
    fn init() -> Result<Self> {
        pr_info!("Rust minimal sample (init)\n");
        pr_info!("Am I built-in? {}\n", !cfg!(MODULE));


        for _ in 0..100 {
            let res = bench_it(&mut ||{
                let x = Vec::<u64>::new();

            });
            pr_alert!("{}\n", res);
        }

        let _a = Box::new(8);

        let mut size = 2;

        for i in 0..8 {
            pr_info!("[module] alloc\n");
            let mut v: Vec<usize> = Vec::new();
            v.push(0);
        }

        Ok(RustMinimal {
            message: "on the heap!".to_owned(),
        })
    }
}

impl Drop for RustMinimal {
    fn drop(&mut self) {
        pr_info!("My message is {}\n", self.message);
        pr_info!("Rust minimal sample (exit)\n");
    }
}
