//! platform independent lock implementation

#[cfg(unix)]
pub mod pthread_thread_local {
    use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use libc::c_void;

    type TlsKey = libc::pthread_key_t;

    static mut PKEY: TlsKey = 0;
    static PKEY_READY: AtomicBool = AtomicBool::new(false);
    static TLS_SAVE_FAILURES: AtomicUsize = AtomicUsize::new(0);

    pub const fn backend_name() -> &'static str {
        "pthread_key_destructor"
    }

    pub fn tls_key_ready() -> bool {
        PKEY_READY.load(Ordering::Acquire)
    }

    pub fn tls_save_failure_count() -> usize {
        TLS_SAVE_FAILURES.load(Ordering::Acquire)
    }

    /// # Safety
    ///
    /// Return the pointer associated with the current thread's destructor key.
    /// This is only a storage accessor; callers must know the pointee type.
    pub unsafe fn load_tls() -> *mut u8 {
        if PKEY_READY.load(Ordering::Acquire) {
            libc::pthread_getspecific(PKEY) as *mut u8
        } else {
            core::ptr::null_mut()
        }
    }

    /// # Safety
    ///
    /// register the cleanup function for tls data
    /// This function is expected to be called once for the whole program
    pub unsafe fn register_tls_key(free_thread_cache: unsafe extern "C" fn(*mut c_void)) {
        let x = free_thread_cache as *const ();
        let ptr: unsafe extern "C" fn(*mut c_void) = core::mem::transmute(x);
        if libc::pthread_key_create(core::ptr::addr_of_mut!(PKEY), Some(ptr)) == 0 {
            PKEY_READY.store(true, Ordering::Release);
        }
    }
    /// # Safety
    ///
    /// put tls ptr into cleanup function chain
    /// This function is expected to be called once per thread
    pub unsafe fn save_tls(ptr: *mut u8) {
        if PKEY_READY.load(Ordering::Acquire) {
            if libc::pthread_setspecific(PKEY, ptr as *const c_void) != 0 {
                TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
            }
        } else {
            TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[cfg(windows)]
pub mod win_thread_local {
    extern crate winapi;
    use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use libc::c_void as libc_c_void;
    use winapi::ctypes::c_void;
    use winapi::um::fibersapi;
    type TlsKey = winapi::shared::minwindef::DWORD;

    const FLS_OUT_OF_INDEXES: TlsKey = !0;

    static mut PKEY: TlsKey = FLS_OUT_OF_INDEXES;
    static mut TLS_DESTRUCTOR: Option<unsafe extern "C" fn(*mut libc_c_void)> = None;
    static PKEY_READY: AtomicBool = AtomicBool::new(false);
    static TLS_SAVE_FAILURES: AtomicUsize = AtomicUsize::new(0);
    #[thread_local]
    static mut TCACHE: *mut u8 = core::ptr::null_mut();

    pub const fn backend_name() -> &'static str {
        "windows_fls_destructor"
    }

    pub fn tls_key_ready() -> bool {
        PKEY_READY.load(Ordering::Acquire)
    }

    pub fn tls_save_failure_count() -> usize {
        TLS_SAVE_FAILURES.load(Ordering::Acquire)
    }

    /// # Safety
    ///
    /// Return the thread-local pointer saved for the current Windows fiber.
    /// This is only a storage accessor; callers must know the pointee type.
    pub unsafe fn load_tls() -> *mut u8 {
        TCACHE
    }

    /// # Safety
    ///
    /// register the cleanup function for tls data
    /// This function is expected to be called once for the whole program
    pub unsafe fn register_tls_key(free_thread_cache: unsafe extern "C" fn(*mut libc_c_void)) {
        unsafe extern "system" fn fls_destructor(value: *mut c_void) {
            if let Some(destructor) = TLS_DESTRUCTOR {
                destructor(value.cast::<libc_c_void>());
            }
        }

        TLS_DESTRUCTOR = Some(free_thread_cache);
        PKEY = fibersapi::FlsAlloc(Some(fls_destructor));
        PKEY_READY.store(PKEY != FLS_OUT_OF_INDEXES, Ordering::Release);
    }

    #[inline]
    unsafe fn save_thread_local_pointer(ptr: *mut u8) -> *mut c_void {
        TCACHE = ptr;
        ptr as *mut c_void
    }

    /// # Safety
    ///
    /// put tls ptr into cleanup function chain
    /// This function is expected to be called once per thread
    pub unsafe fn save_tls(ptr: *mut u8) {
        let value = save_thread_local_pointer(ptr);
        if PKEY_READY.load(Ordering::Acquire) {
            if fibersapi::FlsSetValue(PKEY, value) == 0 {
                TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
            }
        } else {
            TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[cfg(unix)]
pub mod pthread_lock {
    pub type OsLock = libc::pthread_mutex_t;

    pub const SUPPORT_STATIC_INIT: bool = true;
    pub const STATIC_INITIALIZER: OsLock = libc::PTHREAD_MUTEX_INITIALIZER;
    /// # Safety
    ///
    /// dynamically init the mutex thread, some platforms(windows) doesn't have static init
    pub unsafe fn dynamic_initialize(ptr: *mut OsLock) {
        libc::pthread_mutex_init(ptr, core::ptr::null());
    }
    /// # Safety
    ///
    /// lock the mutex
    pub unsafe fn lock(ptr: *mut OsLock) {
        libc::pthread_mutex_lock(ptr);
    }
    /// # Safety
    ///
    /// Attempt to lock the mutex without blocking the current thread.
    pub unsafe fn try_lock(ptr: *mut OsLock) -> bool {
        libc::pthread_mutex_trylock(ptr) == 0
    }
    /// # Safety
    ///
    /// unlock the mutex
    pub unsafe fn unlock(ptr: *mut OsLock) {
        libc::pthread_mutex_unlock(ptr);
    }
}

#[cfg(windows)]
pub mod win_lock {
    pub type OsLock = winapi::um::winnt::RTL_SRWLOCK;

    pub const SUPPORT_STATIC_INIT: bool = true;
    pub const STATIC_INITIALIZER: OsLock = winapi::um::winnt::RTL_SRWLOCK_INIT;
    /// # Safety
    ///
    /// dynamically init the mutex thread, some platforms(windows) doesn't have static init
    pub unsafe fn dynamic_initialize(ptr: *mut OsLock) {
        *ptr = STATIC_INITIALIZER;
    }
    /// # Safety
    ///
    /// lock the mutex
    pub unsafe fn lock(ptr: *mut OsLock) {
        winapi::um::synchapi::AcquireSRWLockExclusive(ptr);
    }
    /// # Safety
    ///
    /// Attempt to lock the mutex without blocking the current thread.
    pub unsafe fn try_lock(ptr: *mut OsLock) -> bool {
        winapi::um::synchapi::TryAcquireSRWLockExclusive(ptr) != 0
    }
    /// # Safety
    ///
    /// unlock the mutex
    pub unsafe fn unlock(ptr: *mut OsLock) {
        winapi::um::synchapi::ReleaseSRWLockExclusive(ptr);
    }
}

#[cfg(unix)]
pub use pthread_lock as general_lock;
#[cfg(unix)]
pub use pthread_thread_local as general_thread_local;
#[cfg(windows)]
pub use win_lock as general_lock;
#[cfg(windows)]
pub use win_thread_local as general_thread_local;
