//! Compile-only MIR fixture for semantic-scope Clone candidate classification.
//!
//! `main` calls every helper through `black_box` so the MIR bodies and call sites
//! remain observable.  The fixture is compiled through the rustc-driver pass,
//! but it is never executed by the validator.

#![feature(bench_black_box)]
#![allow(dead_code, stable_features)]

use std::hint::black_box;

struct NonHeapToken(u64);

struct NonHeapError(u32);

impl Copy for NonHeapToken {}

impl Copy for NonHeapError {}

impl Clone for NonHeapToken {
    #[inline(never)]
    fn clone(&self) -> Self {
        Self(black_box(self.0))
    }
}

impl Clone for NonHeapError {
    #[inline(never)]
    fn clone(&self) -> Self {
        Self(black_box(self.0))
    }
}

#[derive(Clone)]
struct NestedVecOwner {
    values: Vec<u8>,
}

#[derive(Clone, Copy)]
struct RawPointerWrapper(*mut u8);

#[derive(Clone, Copy)]
struct ConstGenericClone<const N: usize>([u8; N]);

struct MultiOwnerStruct<L, R> {
    left: L,
    right: R,
}

enum MultiOwnerEnum<L, R> {
    Left(L),
    Right(R),
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
fn clone_nonheap_option(value: &Option<NonHeapToken>) -> Option<NonHeapToken> {
    <Option<NonHeapToken> as Clone>::clone(value)
}

#[inline(never)]
fn clone_nonheap_result(
    value: &Result<NonHeapToken, NonHeapError>,
) -> Result<NonHeapToken, NonHeapError> {
    <Result<NonHeapToken, NonHeapError> as Clone>::clone(value)
}

#[inline(never)]
fn clone_nonowning_reference(value: &&'static Vec<u8>) -> &'static Vec<u8> {
    <&'static Vec<u8> as Clone>::clone(value)
}

#[inline(never)]
fn clone_ambiguous_result(value: &Result<Vec<u8>, String>) -> Result<Vec<u8>, String> {
    <Result<Vec<u8>, String> as Clone>::clone(value)
}

#[inline(never)]
fn clone_nested_vec_owner(value: &NestedVecOwner) -> NestedVecOwner {
    <NestedVecOwner as Clone>::clone(value)
}

#[inline(never)]
fn clone_raw_pointer_wrapper(value: &RawPointerWrapper) -> RawPointerWrapper {
    <RawPointerWrapper as Clone>::clone(value)
}

#[inline(never)]
fn clone_const_generic<const N: usize>(value: &ConstGenericClone<N>) -> ConstGenericClone<N> {
    <ConstGenericClone<N> as Clone>::clone(value)
}

#[inline(never)]
fn drop_multi_owner_struct(value: MultiOwnerStruct<Vec<u8>, String>) {
    black_box(&value);
}

#[inline(never)]
fn drop_multi_owner_enum(value: MultiOwnerEnum<Vec<u8>, String>) {
    black_box(&value);
}

static EMPTY_VEC: Vec<u8> = Vec::new();

fn main() {
    let single = black_box(None::<Vec<u8>>);
    black_box(clone_single_heap(black_box(&single)));

    let token = black_box(NonHeapToken(7));
    black_box(clone_nonheap_token(black_box(&token)));

    let option = black_box(Some(NonHeapToken(11)));
    black_box(clone_nonheap_option(black_box(&option)));

    let result = black_box(Ok::<NonHeapToken, NonHeapError>(NonHeapToken(13)));
    let _ = black_box(clone_nonheap_result(black_box(&result)));

    let borrowed: &'static Vec<u8> = &EMPTY_VEC;
    black_box(clone_nonowning_reference(black_box(&borrowed)));

    let ambiguous = black_box(Err::<Vec<u8>, String>(String::new()));
    let _ = black_box(clone_ambiguous_result(black_box(&ambiguous)));

    let nested = black_box(NestedVecOwner { values: Vec::new() });
    black_box(clone_nested_vec_owner(black_box(&nested)));

    let raw = black_box(RawPointerWrapper(std::ptr::null_mut()));
    black_box(clone_raw_pointer_wrapper(black_box(&raw)));

    let const_generic = black_box(ConstGenericClone([17_u8; 4]));
    black_box(clone_const_generic(black_box(&const_generic)));

    drop_multi_owner_struct(black_box(MultiOwnerStruct {
        left: Vec::new(),
        right: String::new(),
    }));
    drop_multi_owner_enum(black_box(MultiOwnerEnum::Left(Vec::new())));
}
