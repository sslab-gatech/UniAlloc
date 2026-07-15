//! RUSTSEC-2021-0119 / nix 0.22.1.
//!
//! This adapter replaces libc's `getgrouplist` symbol with a deterministic
//! ABI-compatible shim.  It models the documented glibc retry contract: a
//! short buffer is filled as far as possible, `ngroups` is updated to the
//! required count, and `-1` is returned.  The vulnerable nix retry retains the
//! required count after growing from 8 to 16 entries, so its second call asks
//! the shim to write 20 entries into a 16-entry allocation.  nix 0.22.2 resets
//! `ngroups` from the actual capacity on every iteration and reaches a third,
//! successful call with capacity 32.

use nix::unistd::{getgrouplist as nix_getgrouplist, Gid};
use std::ffi::CString;
use std::sync::atomic::{AtomicUsize, Ordering};

const REQUIRED_GROUPS: libc::c_int = 20;
static CALLS: AtomicUsize = AtomicUsize::new(0);
static SUPPLIED_0: AtomicUsize = AtomicUsize::new(0);
static SUPPLIED_1: AtomicUsize = AtomicUsize::new(0);
static SUPPLIED_2: AtomicUsize = AtomicUsize::new(0);

/// Deterministic stand-in for the platform `getgrouplist(3)` implementation.
///
/// The signature is the Linux libc ABI used by the pinned nix releases.  This
/// deliberately performs the writes requested by the caller; ASan therefore
/// observes nix 0.22.1's length/capacity mismatch at the FFI boundary.
#[no_mangle]
pub unsafe extern "C" fn getgrouplist(
    _user: *const libc::c_char,
    group: libc::gid_t,
    groups: *mut libc::gid_t,
    ngroups: *mut libc::c_int,
) -> libc::c_int {
    let call = CALLS.fetch_add(1, Ordering::SeqCst);
    let supplied = unsafe { *ngroups };
    match call {
        0 => SUPPLIED_0.store(supplied as usize, Ordering::SeqCst),
        1 => SUPPLIED_1.store(supplied as usize, Ordering::SeqCst),
        2 => SUPPLIED_2.store(supplied as usize, Ordering::SeqCst),
        _ => {}
    }

    let writes = supplied.min(REQUIRED_GROUPS);
    for index in 0..writes {
        unsafe {
            groups
                .add(index as usize)
                .write(group + index as libc::gid_t)
        };
    }
    unsafe { *ngroups = REQUIRED_GROUPS };

    if supplied >= REQUIRED_GROUPS {
        REQUIRED_GROUPS
    } else {
        -1
    }
}

fn main() {
    let user = CString::new("unialloc-rustsec-synthetic-user").unwrap();
    let groups = nix_getgrouplist(&user, Gid::from_raw(1000)).unwrap();

    assert_eq!(groups.len(), REQUIRED_GROUPS as usize);
    for (index, gid) in groups.iter().enumerate() {
        assert_eq!(gid.as_raw(), 1000 + index as libc::gid_t);
    }
    assert_eq!(CALLS.load(Ordering::SeqCst), 3);
    assert_eq!(SUPPLIED_0.load(Ordering::SeqCst), 8);
    assert_eq!(SUPPLIED_1.load(Ordering::SeqCst), 16);
    assert_eq!(SUPPLIED_2.load(Ordering::SeqCst), 32);
}
