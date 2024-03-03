pub use crate::mm::BackendAllocator as GlobalBackend;

pub use alloc_macros::atomic_static;

pub use core::intrinsics::{likely, unlikely};

pub use crate::size_class::{
    get_num_pages_by_idx, get_rounded_size, get_rounded_size_by_idx, get_size_class,
    get_size_class_tuple, SizeClass, MAX_SIZE, TOTAL_SIZE_CLASS,
};
// error codes
// pub use super::error::{Result, AllocError};
