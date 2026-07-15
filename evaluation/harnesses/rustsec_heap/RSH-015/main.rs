//! RUSTSEC-2022-0012 / arrow2 0.7.0.
//!
//! Unsafe setup is required by Arrow's FFI export contract.  The vulnerable
//! operation itself is the safe `Clone` implementation on `Ffi_ArrowArray`,
//! which shallow-copies the private owning pointer and release callback.

use arrow2::array::UInt32Array;
use arrow2::ffi::{export_array_to_c, Ffi_ArrowArray};
use std::sync::Arc;

fn main() {
    let array = Arc::new(UInt32Array::from_slice([1, 2, 3, 4]));
    let mut ffi_array = Ffi_ArrowArray::empty();
    unsafe { export_array_to_c(array, &mut ffi_array as *mut _) };
    let _duplicate = ffi_array.clone();
}
