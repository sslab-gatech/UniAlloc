//! Revision-bound Rust `GlobalAlloc` adapter for the modern google/tcmalloc
//! artifact built by `evaluation/scripts/build_google_tcmalloc.py`.

#[cfg(not(target_os = "linux"))]
compile_error!("bench_tcmalloc requires the pinned Linux google/tcmalloc artifact");
#[cfg(any(
    feature = "bench_ourself",
    feature = "bench_ptmalloc",
    feature = "bench_jemalloc",
    feature = "bench_mimalloc",
    feature = "bench_gperftools_legacy",
    feature = "bench_snmalloc",
    feature = "bench_scudo"
))]
compile_error!("bench_tcmalloc must be the only enabled benchmark allocator feature");

use core::alloc::{GlobalAlloc, Layout};
use core::ffi::{c_char, c_int, c_void};

const EXPECTED_REVISION: &[u8] = b"12f255231938d30493186b0a037feedd70f5a1c1\0";
const FAILURE: &[u8] = b"unialloc: modern google/tcmalloc HPAA identity check failed\n";

#[link(name = "unialloc_google_tcmalloc")]
extern "C" {
    fn unialloc_google_tcmalloc_alloc(size: usize, alignment: usize) -> *mut c_void;
    fn unialloc_google_tcmalloc_dealloc(ptr: *mut c_void);
    fn unialloc_google_tcmalloc_revision() -> *const c_char;
    fn unialloc_google_tcmalloc_hpaa_active() -> c_int;
    fn unialloc_google_tcmalloc_malloc_provider_is_self() -> c_int;
    fn unialloc_google_tcmalloc_revision_12f255231938d30493186b0a037feedd70f5a1c1();
}

pub struct GoogleTcmalloc;

unsafe impl GlobalAlloc for GoogleTcmalloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        unialloc_google_tcmalloc_alloc(layout.size().max(1), layout.align()).cast()
    }

    unsafe fn dealloc(&self, ptr: *mut u8, _layout: Layout) {
        unialloc_google_tcmalloc_dealloc(ptr.cast());
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let ptr = self.alloc(layout);
        if !ptr.is_null() {
            core::ptr::write_bytes(ptr, 0, layout.size());
        }
        ptr
    }
}

unsafe fn revision_matches(mut actual: *const c_char) -> bool {
    if actual.is_null() {
        return false;
    }
    for expected in EXPECTED_REVISION {
        if *actual.cast::<u8>() != *expected {
            return false;
        }
        if *expected == 0 {
            return true;
        }
        actual = actual.add(1);
    }
    false
}

unsafe extern "C" fn verify_google_tcmalloc() {
    // Referencing the revision-named symbol makes a gperftools `libtcmalloc`
    // incapable of satisfying this benchmark at link time.
    unialloc_google_tcmalloc_revision_12f255231938d30493186b0a037feedd70f5a1c1();
    let valid = revision_matches(unialloc_google_tcmalloc_revision())
        && unialloc_google_tcmalloc_hpaa_active() == 1
        && unialloc_google_tcmalloc_malloc_provider_is_self() == 1;
    if !valid {
        libc::write(
            libc::STDERR_FILENO,
            FAILURE.as_ptr().cast::<c_void>(),
            FAILURE.len(),
        );
        libc::_exit(86);
    }
}

// Run after the loader has mapped the link-time artifact and before the Rust
// benchmark harness can produce a measurement with the wrong allocator.
#[cfg(target_os = "linux")]
#[used]
#[link_section = ".init_array"]
static VERIFY_GOOGLE_TCMALLOC: unsafe extern "C" fn() = verify_google_tcmalloc;
