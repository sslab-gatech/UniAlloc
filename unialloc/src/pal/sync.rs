//! platform independent lock implementation

#[cfg(unix)]
pub mod pthread_thread_local {
    use libc::c_void;

    type TlsKey = libc::pthread_key_t;

    static mut PKEY: TlsKey = 0;

    /// # Safety
    ///
    /// register the cleanup function for tls data
    /// This function is expected to be called once for the whole program
    pub unsafe fn register_tls_key(free_thread_cache: unsafe extern "C" fn(*mut c_void)) {
        let x = free_thread_cache as *const ();
        let ptr: unsafe extern "C" fn(*mut c_void) = core::mem::transmute(x);
        libc::pthread_key_create(&PKEY as *const _ as *mut TlsKey, Some(ptr));
    }
    /// # Safety
    ///
    /// put tls ptr into cleanup function chain
    /// This function is expected to be called once per thread
    pub unsafe fn save_tls(ptr: *mut u8) {
        libc::pthread_setspecific(PKEY, ptr as *const c_void);
    }
}

#[cfg(windows)]
pub mod win_thread_local {
    extern crate winapi;
    use winapi::ctypes::c_void;
    use winapi::um::fibersapi;
    type TlsKey = winapi::shared::minwindef::DWORD;

    static mut PKEY: TlsKey = 0;
    #[thread_local]
    static mut TCACHE: *mut u8 = core::ptr::null_mut();

    /// # Safety
    ///
    /// register the cleanup function for tls data
    /// This function is expected to be called once for the whole program
    pub unsafe fn register_tls_key(free_thread_cache: unsafe extern "C" fn(*mut c_void)) {
        let x = free_thread_cache as *const ();
        let ptr: unsafe extern "system" fn(*mut c_void) = core::mem::transmute(x);
        PKEY = fibersapi::FlsAlloc(Some(ptr));
    }
    /// # Safety
    ///
    /// put tls ptr into cleanup function chain
    /// This function is expected to be called once per thread
    pub unsafe fn save_tls(ptr: *mut u8) {
        fibersapi::FlsSetValue(PKEY, TCACHE as *mut winapi::ctypes::c_void);
        TCACHE = ptr;
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
