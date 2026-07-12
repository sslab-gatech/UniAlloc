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
    pub unsafe fn register_tls_key(free_thread_cache: unsafe extern "C" fn(*mut c_void)) -> bool {
        let x = free_thread_cache as *const ();
        let ptr: unsafe extern "C" fn(*mut c_void) = core::mem::transmute(x);
        let registered = libc::pthread_key_create(core::ptr::addr_of_mut!(PKEY), Some(ptr)) == 0;
        PKEY_READY.store(registered, Ordering::Release);
        registered
    }
    /// # Safety
    ///
    /// put tls ptr into cleanup function chain
    /// This function is expected to be called once per thread
    pub unsafe fn save_tls(ptr: *mut u8) -> bool {
        if PKEY_READY.load(Ordering::Acquire) {
            let saved = libc::pthread_setspecific(PKEY, ptr as *const c_void) == 0;
            if !saved {
                TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
            }
            saved
        } else {
            TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
            false
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
    #[cfg(test)]
    static FAIL_NEXT_TLS_REGISTRATION: AtomicBool = AtomicBool::new(false);
    #[cfg(test)]
    static FAIL_NEXT_TLS_SAVE: AtomicBool = AtomicBool::new(false);
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
    /// Return the fiber-local pointer saved for the current Windows fiber.
    /// This is only a storage accessor; callers must know the pointee type.
    pub unsafe fn load_tls() -> *mut u8 {
        if PKEY_READY.load(Ordering::Acquire) {
            fibersapi::FlsGetValue(PKEY).cast::<u8>()
        } else {
            core::ptr::null_mut()
        }
    }

    /// # Safety
    ///
    /// register the cleanup function for tls data
    /// This function is expected to be called once for the whole program
    pub unsafe fn register_tls_key(
        free_thread_cache: unsafe extern "C" fn(*mut libc_c_void),
    ) -> bool {
        unsafe extern "system" fn fls_destructor(value: *mut c_void) {
            if let Some(destructor) = TLS_DESTRUCTOR {
                destructor(value.cast::<libc_c_void>());
            }
        }

        #[cfg(test)]
        if FAIL_NEXT_TLS_REGISTRATION.swap(false, Ordering::AcqRel) {
            PKEY_READY.store(false, Ordering::Release);
            return false;
        }

        let key = fibersapi::FlsAlloc(Some(fls_destructor));
        if key == FLS_OUT_OF_INDEXES {
            PKEY = FLS_OUT_OF_INDEXES;
            TLS_DESTRUCTOR = None;
            PKEY_READY.store(false, Ordering::Release);
            return false;
        }
        TLS_DESTRUCTOR = Some(free_thread_cache);
        PKEY = key;
        PKEY_READY.store(true, Ordering::Release);
        true
    }

    /// # Safety
    ///
    /// put tls ptr into cleanup function chain
    /// This function is expected to be called once per fiber
    pub unsafe fn save_tls(ptr: *mut u8) -> bool {
        #[cfg(test)]
        if FAIL_NEXT_TLS_SAVE.swap(false, Ordering::AcqRel) {
            TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
            return false;
        }

        if PKEY_READY.load(Ordering::Acquire) {
            let saved = fibersapi::FlsSetValue(PKEY, ptr.cast::<c_void>()) != 0;
            if !saved {
                TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
            }
            saved
        } else {
            TLS_SAVE_FAILURES.fetch_add(1, Ordering::Relaxed);
            false
        }
    }

    #[cfg(test)]
    pub(crate) fn fail_next_tls_registration_for_test() {
        FAIL_NEXT_TLS_REGISTRATION.store(true, Ordering::Release);
    }

    #[cfg(test)]
    pub(crate) fn fail_next_tls_save_for_test() {
        FAIL_NEXT_TLS_SAVE.store(true, Ordering::Release);
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use core::ptr;
        use winapi::um::winbase::{
            ConvertFiberToThread, ConvertThreadToFiber, CreateFiber, DeleteFiber, SwitchToFiber,
        };

        static MAIN_FIBER: AtomicUsize = AtomicUsize::new(0);
        static FIBER_B_INITIAL_VALUE: AtomicUsize = AtomicUsize::new(usize::MAX);
        static FIBER_B_SAVED_VALUE: AtomicUsize = AtomicUsize::new(usize::MAX);
        static FIBER_A_DESTRUCTOR_CALLS: AtomicUsize = AtomicUsize::new(0);
        static FIBER_B_DESTRUCTOR_CALLS: AtomicUsize = AtomicUsize::new(0);
        static FIBER_A_VALUE: u8 = 0;
        static FIBER_B_VALUE: u8 = 0;

        unsafe extern "C" fn record_destructor(value: *mut libc_c_void) {
            if value.cast::<u8>() == ptr::addr_of!(FIBER_A_VALUE).cast_mut() {
                FIBER_A_DESTRUCTOR_CALLS.fetch_add(1, Ordering::Relaxed);
            } else if value.cast::<u8>() == ptr::addr_of!(FIBER_B_VALUE).cast_mut() {
                FIBER_B_DESTRUCTOR_CALLS.fetch_add(1, Ordering::Relaxed);
            }
        }

        unsafe extern "system" fn fiber_b_entry(_parameter: *mut c_void) {
            FIBER_B_INITIAL_VALUE.store(load_tls() as usize, Ordering::Release);
            assert!(save_tls(ptr::addr_of!(FIBER_B_VALUE).cast_mut()));
            FIBER_B_SAVED_VALUE.store(load_tls() as usize, Ordering::Release);
            SwitchToFiber(MAIN_FIBER.load(Ordering::Acquire) as *mut c_void);
        }

        #[test]
        #[ignore = "mutates the process-wide FLS key; run this test in an isolated Windows process"]
        fn fls_values_and_destructor_ownership_are_fiber_local() {
            unsafe {
                FIBER_B_INITIAL_VALUE.store(usize::MAX, Ordering::Relaxed);
                FIBER_B_SAVED_VALUE.store(usize::MAX, Ordering::Relaxed);
                FIBER_A_DESTRUCTOR_CALLS.store(0, Ordering::Relaxed);
                FIBER_B_DESTRUCTOR_CALLS.store(0, Ordering::Relaxed);

                let main_fiber = ConvertThreadToFiber(ptr::null_mut());
                assert!(!main_fiber.is_null(), "ConvertThreadToFiber failed");

                if !register_tls_key(record_destructor) {
                    let _ = ConvertFiberToThread();
                    panic!("FlsAlloc failed");
                }

                let fiber_a_value = ptr::addr_of!(FIBER_A_VALUE).cast_mut();
                let fiber_b_value = ptr::addr_of!(FIBER_B_VALUE).cast_mut();
                assert!(save_tls(fiber_a_value));
                MAIN_FIBER.store(main_fiber as usize, Ordering::Release);

                let fiber_b = CreateFiber(0, Some(fiber_b_entry), ptr::null_mut());
                if fiber_b.is_null() {
                    let _ = fibersapi::FlsSetValue(PKEY, ptr::null_mut());
                    let _ = fibersapi::FlsFree(PKEY);
                    PKEY = FLS_OUT_OF_INDEXES;
                    PKEY_READY.store(false, Ordering::Release);
                    TLS_DESTRUCTOR = None;
                    let _ = ConvertFiberToThread();
                    panic!("CreateFiber failed");
                }

                SwitchToFiber(fiber_b);
                let fiber_a_after_switch = load_tls();
                DeleteFiber(fiber_b);

                let fiber_b_initial = FIBER_B_INITIAL_VALUE.load(Ordering::Acquire);
                let fiber_b_saved = FIBER_B_SAVED_VALUE.load(Ordering::Acquire);
                let fiber_a_destructor_calls = FIBER_A_DESTRUCTOR_CALLS.load(Ordering::Acquire);
                let fiber_b_destructor_calls = FIBER_B_DESTRUCTOR_CALLS.load(Ordering::Acquire);

                assert_ne!(fibersapi::FlsSetValue(PKEY, ptr::null_mut()), 0);
                assert_ne!(fibersapi::FlsFree(PKEY), 0);
                PKEY = FLS_OUT_OF_INDEXES;
                PKEY_READY.store(false, Ordering::Release);
                TLS_DESTRUCTOR = None;
                assert_ne!(ConvertFiberToThread(), 0);

                assert_eq!(fiber_b_initial, 0, "new fiber inherited fiber A's value");
                assert_eq!(fiber_b_saved, fiber_b_value as usize);
                assert_eq!(fiber_a_after_switch, fiber_a_value);
                assert_eq!(fiber_a_destructor_calls, 0);
                assert_eq!(fiber_b_destructor_calls, 1);
            }
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
