//! Arena and Backend Allocators

use crate::buddy_system::*;
use crate::prelude::*;
use alloc::boxed::Box;
use core::alloc::{AllocError, Allocator, Layout};
use core::ptr::NonNull;
use core::sync::atomic::{AtomicPtr, Ordering};
use spin::Mutex;

pub use crate::freelist::BuddySystemAllocator as BackendAllocator;

pub mod linklist;
