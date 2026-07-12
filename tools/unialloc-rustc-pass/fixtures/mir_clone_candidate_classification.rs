//! Compile-only MIR fixture for semantic-scope Clone candidate classification.
//!
//! `main` retains each helper as a function pointer.  The fixture is compiled
//! through the rustc-driver pass, but it is never executed by the validator.

use std::hint::black_box;

struct NonHeapToken;

impl Clone for NonHeapToken {
    #[inline(never)]
    fn clone(&self) -> Self {
        Self
    }
}

#[inline(never)]
fn clone_single_heap(value: &Option<Vec<u8>>) -> Option<Vec<u8>> {
    <Option<Vec<u8>> as Clone>::clone(value)
}

#[inline(never)]
fn clone_nonheap_token(value: &NonHeapToken) -> NonHeapToken {
    <NonHeapToken as Clone>::clone(value)
}

#[inline(never)]
fn clone_nonheap_option(value: &Option<u8>) -> Option<u8> {
    <Option<u8> as Clone>::clone(value)
}

#[inline(never)]
fn clone_nonheap_result(value: &Result<u16, u32>) -> Result<u16, u32> {
    <Result<u16, u32> as Clone>::clone(value)
}

#[inline(never)]
fn clone_nonowning_reference(value: &&'static Vec<u8>) -> &'static Vec<u8> {
    <&'static Vec<u8> as Clone>::clone(value)
}

#[inline(never)]
fn clone_ambiguous_result(value: &Result<Vec<u8>, String>) -> Result<Vec<u8>, String> {
    <Result<Vec<u8>, String> as Clone>::clone(value)
}

fn main() {
    black_box(clone_single_heap as fn(&Option<Vec<u8>>) -> Option<Vec<u8>>);
    black_box(clone_nonheap_token as fn(&NonHeapToken) -> NonHeapToken);
    black_box(clone_nonheap_option as fn(&Option<u8>) -> Option<u8>);
    black_box(clone_nonheap_result as fn(&Result<u16, u32>) -> Result<u16, u32>);
    black_box(clone_nonowning_reference as fn(&&'static Vec<u8>) -> &'static Vec<u8>);
    black_box(clone_ambiguous_result as fn(&Result<Vec<u8>, String>) -> Result<Vec<u8>, String>);
}
