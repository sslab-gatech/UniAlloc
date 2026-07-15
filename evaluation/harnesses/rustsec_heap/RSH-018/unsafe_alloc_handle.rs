//! RUSTSEC-2025-0053 / arenavec issue 4: a safe trait implementation can
//! return an arbitrary pointer later dereferenced by a safe SliceVec API.

#![forbid(unsafe_code)]

use std::ptr::NonNull;

struct Payload(String);

#[derive(Clone)]
struct ForgedHandle(String);

impl arenavec::common::AllocHandle for ForgedHandle {
    fn allocate<T>(&self, _: usize) -> NonNull<T> {
        std::hint::black_box(&self.0);
        NonNull::new(0x1234_5678_9abc as *mut T).expect("non-null forged address")
    }

    fn allocate_or_extend<T>(&self, _: NonNull<T>, _: usize, _: usize) -> NonNull<T> {
        panic!("unused")
    }
}

fn main() {
    let mut values = arenavec::common::SliceVec::<Payload, ForgedHandle>::with_capacity(
        ForgedHandle(String::from("BUG")),
        1,
    );
    values.push(Payload(String::new()));
}
