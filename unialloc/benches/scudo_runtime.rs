#[cfg(any(
    feature = "bench_ourself",
    feature = "bench_ptmalloc",
    feature = "bench_jemalloc",
    feature = "bench_mimalloc",
    feature = "bench_tcmalloc",
    feature = "bench_gperftools_legacy",
    feature = "bench_snmalloc"
))]
compile_error!("bench_scudo must be the only enabled bench allocator feature");

#[cfg(not(target_os = "linux"))]
compile_error!("bench_scudo requires Linux and a verifiable Scudo runtime");

#[cfg(target_os = "linux")]
mod linux {
    use core::ffi::c_void;
    use core::mem::MaybeUninit;
    use core::ptr;

    const EXIT_SCUDO_UNVERIFIED: libc::c_int = 86;
    const SUCCESS: &[u8] = b"unialloc: verified Scudo runtime identity\n";
    const FAILURE: &[u8] = b"unialloc: bench_scudo requires a verified Scudo runtime\n";
    const SCUDO_IDENTITY_SYMBOL: &[u8] = b"__scudo_print_stats\0";
    const SCUDO_RUNTIME_LIBRARY_ENV: &[u8] = b"UNIALLOC_SCUDO_RUNTIME_LIBRARY\0";
    const PATH_CAPACITY: usize = 4096;
    const ALLOCATOR_ABI_SYMBOLS: [&[u8]; 5] = [
        b"malloc\0",
        b"calloc\0",
        b"realloc\0",
        b"free\0",
        b"posix_memalign\0",
    ];

    fn emit(message: &[u8]) {
        // Keep the constructor allocation-free: stdio and formatting may allocate.
        unsafe {
            libc::write(
                libc::STDERR_FILENO,
                message.as_ptr().cast::<c_void>(),
                message.len(),
            );
        }
    }

    fn fail_closed() -> ! {
        emit(FAILURE);
        unsafe { libc::_exit(EXIT_SCUDO_UNVERIFIED) }
    }

    fn symbol_provider(symbol: &[u8]) -> Option<libc::Dl_info> {
        // RTLD_DEFAULT is a null handle on Linux. Resolve the process-wide winner
        // so a loaded-but-shadowed Scudo DSO cannot satisfy this guard.
        let address =
            unsafe { libc::dlsym(ptr::null_mut(), symbol.as_ptr().cast::<libc::c_char>()) };
        if address.is_null() {
            return None;
        }

        let mut info = MaybeUninit::<libc::Dl_info>::uninit();
        if unsafe { libc::dladdr(address as *const c_void, info.as_mut_ptr()) } == 0 {
            return None;
        }
        Some(unsafe { info.assume_init() })
    }

    fn same_c_path(left: &[libc::c_char], right: &[libc::c_char]) -> bool {
        for index in 0..left.len().min(right.len()) {
            if left[index] != right[index] {
                return false;
            }
            if left[index] == 0 {
                return true;
            }
        }
        false
    }

    fn provider_matches_configured_runtime(provider: &libc::Dl_info) -> bool {
        let configured =
            unsafe { libc::getenv(SCUDO_RUNTIME_LIBRARY_ENV.as_ptr().cast::<libc::c_char>()) };
        // The sanitizer route links Scudo into the executable and does not set
        // the standalone-runtime environment variable. The LD_PRELOAD route
        // always sets it and must bind the winning provider to that exact file.
        if configured.is_null() || unsafe { *configured } == 0 {
            return true;
        }
        if provider.dli_fname.is_null() {
            return false;
        }
        let mut configured_realpath = [0 as libc::c_char; PATH_CAPACITY];
        let mut provider_realpath = [0 as libc::c_char; PATH_CAPACITY];
        if unsafe { libc::realpath(configured, configured_realpath.as_mut_ptr()) }.is_null()
            || unsafe { libc::realpath(provider.dli_fname, provider_realpath.as_mut_ptr()) }
                .is_null()
        {
            return false;
        }
        same_c_path(&configured_realpath, &provider_realpath)
    }

    extern "C" fn verify_scudo_runtime_identity() {
        let scudo_provider =
            symbol_provider(SCUDO_IDENTITY_SYMBOL).unwrap_or_else(|| fail_closed());
        if !provider_matches_configured_runtime(&scudo_provider) {
            fail_closed();
        }

        for symbol in ALLOCATOR_ABI_SYMBOLS {
            let provider = symbol_provider(symbol).unwrap_or_else(|| fail_closed());
            if provider.dli_fbase != scudo_provider.dli_fbase
                || !provider_matches_configured_runtime(&provider)
            {
                fail_closed();
            }
        }

        emit(SUCCESS);
    }

    #[used]
    #[link_section = ".init_array"]
    static VERIFY_SCUDO_RUNTIME_IDENTITY: extern "C" fn() = verify_scudo_runtime_identity;
}
