//! RUSTSEC-2020-0017 / internment 0.3.13.
//!
//! This mechanical standalone adapter preserves the concurrent allocation and
//! drop loop from the regression test added by upstream PR #14.  Under ASan,
//! the vulnerable release reports a heap-use-after-free in `ArcIntern::drop`;
//! internment 0.4.0 completes without a sanitizer finding.

use internment::ArcIntern;
use std::thread;

#[derive(Eq, Hash, PartialEq)]
struct TestStruct(String, u64);

fn main() {
    let mut threads = Vec::new();
    for _ in 0..10 {
        threads.push(thread::spawn(|| {
            for _ in 0..100_000 {
                let _first = ArcIntern::new(TestStruct("foo".to_string(), 5));
                let _second = ArcIntern::new(TestStruct("bar".to_string(), 10));
            }
        }));
    }

    for thread in threads {
        thread.join().expect("worker must complete");
    }
}
