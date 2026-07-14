use proc_macro::{token_stream, Group, TokenStream, TokenTree};

fn try_ident(it: &mut token_stream::IntoIter) -> Option<String> {
    if let Some(TokenTree::Ident(ident)) = it.next() {
        Some(ident.to_string())
    } else {
        None
    }
}

fn expect_ident(it: &mut token_stream::IntoIter) -> String {
    try_ident(it).expect("Expected Ident")
}

fn expect_punct(it: &mut token_stream::IntoIter) -> char {
    if let TokenTree::Punct(punct) = it.next().expect("Reached end of token stream for Punct") {
        punct.as_char()
    } else {
        panic!("Expected Punct");
    }
}

fn expect_group(it: &mut token_stream::IntoIter) -> Group {
    if let TokenTree::Group(group) = it.next().expect("Reached end of token stream for Group") {
        group
    } else {
        panic!("Expected Group");
    }
}

// lazy static built on top of [`Atomics`] variables
//
// # Examples
//
// ```rust,no_run
// extern crate alloc;
// // use global allocator
// use alloc::alloc::Global as GlobalBackend;
// // use a custom allocator
// use your_cool_path::your_cool_allocator as GlobalBackend;
//
// atomic_static! {
//     [pub] static ref EXAMPLE: u8 = { let x=1; x*2 };
// }
// ```
//
// At the moment, the curly braces around the `let x=1; x*2` and the semicolon
// after the right curly brace is required.
//
// # Note
//
// The internal representation requires `Box` to allocate objects on the heap.
// However, large objects can potentially overflow the stack.
// The bug is fixed in 2021-03-12 toolchain.
// See https://github.com/rust-lang/rust/issues/53827.
pub fn atomic_static(input: TokenStream) -> TokenStream {
    let mut it = input.into_iter();
    let first_ident = try_ident(&mut it).unwrap();
    let mut visibility = String::from("");

    if let "pub" = &*first_ident {
        visibility = String::from("pub");
        assert_eq!(expect_ident(&mut it), "static");
    }

    assert_eq!(expect_ident(&mut it), "ref");
    let name = expect_ident(&mut it);
    assert_eq!(expect_punct(&mut it), ':');
    let ty = expect_ident(&mut it);
    assert_eq!(expect_punct(&mut it), '=');
    let init_expr = expect_group(&mut it).to_string();
    assert_eq!(expect_punct(&mut it), ';');

    let expanded = format!(
        "
            // The `VALUE` needs to be declared at the outside of [`deref`]
            // and [`deref_mut`]
            static {name}_VALUE: core::sync::atomic::AtomicPtr<{ty}> = 
                core::sync::atomic::AtomicPtr::new(core::ptr::null_mut());

            // ZST for dereference
            {visibility} struct {name};

            impl core::ops::Deref for {name} {{
                type Target = {ty};

                fn deref(&self) -> &'static {ty} {{
                    extern crate alloc;
                    use alloc::alloc::GlobalAlloc;
                    use alloc::boxed::Box;

                    let mut ptr: *mut {ty} = {name}_VALUE.load(core::sync::atomic::Ordering::Acquire);
                    if !ptr.is_null() {{
                        return unsafe {{ ptr.as_ref().unwrap() }};
                    }}

                    // let boxed_ptr:Box<{ty}, GlobalBackend> =
                    //     Box::new_in(
                    //         {init_expr}
                    //         , GlobalBackend);
                    // let init_ptr = Box::into_raw(boxed_ptr);

                    let layout = alloc::alloc::Layout::new::<{ty}>();
                    let init_ptr = unsafe {{ META_BUMP.lock().alloc(layout.size()).expect(\"err\")  as *mut {ty} }};
                    unsafe{{ core::ptr::write(init_ptr as * mut {ty}, {init_expr}) }};

                    if let Err(p) = {name}_VALUE.compare_exchange(
                        core::ptr::null_mut(),
                        init_ptr,
                        core::sync::atomic::Ordering::AcqRel,
                        core::sync::atomic::Ordering::Relaxed) {{
                        if ! p.is_null() {{
                            unsafe {{
                                Box::from_raw_in(init_ptr as *mut {ty}, GlobalBackend);
                                // GlobalBackend.dealloc(init_ptr as *mut u8, layout);
                                return p.as_ref().unwrap();
                            }}
                        }}
                    }}
                    unsafe {{
                        init_ptr.as_ref().unwrap()
                    }}
                }}
            }}

            impl core::ops::DerefMut for {name} {{
                fn deref_mut(&mut self) -> &'static mut {ty} {{
                    extern crate alloc;
                    use alloc::alloc::GlobalAlloc;
                    use alloc::boxed::Box;

                    let mut ptr: *mut {ty} = {name}_VALUE.load(core::sync::atomic::Ordering::Acquire);
                    if !ptr.is_null() {{
                        return unsafe {{ ptr.as_mut().unwrap() }};
                    }}
                    // let boxed_ptr:Box<{ty}, GlobalBackend> =
                    //     Box::new_in(
                    //         {init_expr}
                    //         , GlobalBackend);
                    // let init_ptr = Box::into_raw(boxed_ptr);

                    let layout = alloc::alloc::Layout::new::<{ty}>();
                    let init_ptr = unsafe {{ META_BUMP.lock().alloc(layout.size()).expect(\"err\")  as *mut {ty} }};
                    unsafe{{ core::ptr::write(init_ptr as * mut {ty}, {init_expr}) }};

                    if let Err(p) = {name}_VALUE.compare_exchange(
                        core::ptr::null_mut(),
                        init_ptr,
                        core::sync::atomic::Ordering::AcqRel,
                        core::sync::atomic::Ordering::Relaxed) {{
                        if ! p.is_null() {{
                            unsafe {{
                                Box::from_raw_in(init_ptr as *mut {ty}, GlobalBackend);
                                // GlobalBackend.dealloc(init_ptr as *mut u8, layout);
                                return p.as_mut().unwrap();
                            }}
                        }}
                    }}
                    unsafe {{
                        init_ptr.as_mut().unwrap()
                    }}
                }}
            }}
        ",
        ty = ty,
        name = name,
        visibility = visibility,
        init_expr = init_expr,
    );

    expanded
        .parse()
        .expect("Error parsing formatted string into token stream.")
}

pub fn tls_static(input: TokenStream) -> TokenStream {
    let mut it = input.into_iter();

    let ty = expect_ident(&mut it);
    let name = expect_ident(&mut it);
    assert_eq!(expect_punct(&mut it), ',');
    let func = expect_ident(&mut it);
    let tsd_state_name = format!("{}_TSD_STATE", name.to_uppercase());
    let ensure_tsd_name = format!("{}_ensure_tsd_initialized", name.to_lowercase());
    let reset_tsd_name = format!(
        "{}_reset_failed_tsd_initialization_for_test",
        name.to_lowercase()
    );
    let load_tls_name = format!("{}_load_tls_value", name.to_lowercase());
    let store_tls_name = format!("{}_store_tls_value", name.to_lowercase());
    let reclaim_tls_name = format!("{}_reclaim_unpublished_tls_value", name.to_lowercase());
    let tls_storage_failure_name = format!("{}_tls_storage_failure", name.to_lowercase());

    let expanded = format!(
        "
            // The `VALUE` needs to be declared at the outside of [`deref`]
            // and [`deref_mut`].  arm64e current-nightly Mach-O TLV
            // descriptors can fault before allocator logic in no_std probes,
            // so that target uses the already-registered pthread key itself as
            // the per-thread pointer store. Windows also needs its FLS slot to
            // be authoritative because Rust `#[thread_local]` storage is shared
            // by all fibers on one thread. Other targets keep the fast Rust TLS
            // path and still save the pointer into the pthread key for cleanup.
            #[cfg(not(any(unialloc_target_arm64e, windows)))]
            #[thread_local]
            static mut {name}_VALUE: * mut {ty} = core::ptr::null_mut();
            static {tsd_state_name}: core::sync::atomic::AtomicU8 =
                core::sync::atomic::AtomicU8::new(0);

            #[inline]
            unsafe fn {ensure_tsd_name}() -> bool {{
                loop {{
                    match {tsd_state_name}.load(core::sync::atomic::Ordering::Acquire) {{
                        2 => return true,
                        3 => return false,
                        0 => {{
                            if {tsd_state_name}.compare_exchange(
                                0,
                                1,
                                core::sync::atomic::Ordering::AcqRel,
                                core::sync::atomic::Ordering::Acquire,
                            ).is_ok() {{
                                let registered = register_tls_key({func});
                                {tsd_state_name}.store(
                                    if registered {{ 2 }} else {{ 3 }},
                                    core::sync::atomic::Ordering::Release,
                                );
                                return registered;
                            }}
                        }}
                        _ => core::hint::spin_loop(),
                    }}
                }}
            }}

            #[cfg(test)]
            unsafe fn {reset_tsd_name}() -> bool {{
                {tsd_state_name}.compare_exchange(
                    3,
                    0,
                    core::sync::atomic::Ordering::AcqRel,
                    core::sync::atomic::Ordering::Acquire,
                ).is_ok()
            }}

            #[cold]
            #[inline(never)]
            fn {tls_storage_failure_name}() -> ! {{
                extern crate alloc;
                alloc::alloc::handle_alloc_error(alloc::alloc::Layout::new::<{ty}>())
            }}

            #[inline]
            unsafe fn {reclaim_tls_name}(ptr: *mut {ty}) {{
                if ptr.is_null() {{
                    return;
                }}

                extern crate alloc;
                use alloc::alloc::Allocator;
                let layout = alloc::alloc::Layout::new::<{ty}>();
                core::ptr::drop_in_place(ptr);
                MetadataAllocator {{}}.deallocate(
                    core::ptr::NonNull::new_unchecked(ptr.cast::<u8>()),
                    layout,
                );
            }}

            #[inline]
            unsafe fn {load_tls_name}() -> *mut {ty} {{
                #[cfg(any(unialloc_target_arm64e, windows))]
                {{
                    if !{ensure_tsd_name}() {{
                        {tls_storage_failure_name}();
                    }}
                    load_tls() as *mut {ty}
                }}
                #[cfg(not(any(unialloc_target_arm64e, windows)))]
                {{
                    {name}_VALUE
                }}
            }}

            #[inline]
            unsafe fn {store_tls_name}(ptr: *mut {ty}) -> core::result::Result<(), *mut {ty}> {{
                #[cfg(any(unialloc_target_arm64e, windows))]
                {{
                    if !{ensure_tsd_name}() || !save_tls(ptr as *mut u8) {{
                        core::result::Result::Err(ptr)
                    }} else {{
                        core::result::Result::Ok(())
                    }}
                }}
                #[cfg(not(any(unialloc_target_arm64e, windows)))]
                {{
                    // The pthread key owns thread-exit cleanup. Do not publish
                    // the fast Rust TLS pointer unless destructor ownership is
                    // registered and the matching pthread slot accepted it.
                    // Otherwise the cache would remain reachable during the
                    // thread lifetime but leak when Rust TLS is torn down.
                    if !{ensure_tsd_name}() || !save_tls(ptr as *mut u8) {{
                        core::result::Result::Err(ptr)
                    }} else {{
                        {name}_VALUE = ptr;
                        core::result::Result::Ok(())
                    }}
                }}
            }}

            // ZST for dereference
            pub struct {name};

            impl core::ops::Deref for {name} {{
                type Target = {ty};

                fn deref(&self) -> &{ty} {{
                    extern crate alloc;
                    use alloc::alloc::Allocator;
                    unsafe {{
                        let mut ptr = {load_tls_name}();
                        if !ptr.is_null() {{
                            return ptr.as_ref().unwrap();
                        }}
                        let layout = alloc::alloc::Layout::new::<{ty}>();
                        let allocation = MetadataAllocator  {{ }}
                            .allocate(layout)
                            .unwrap_or_else(|_| alloc::alloc::handle_alloc_error(layout));
                        ptr = allocation.as_non_null_ptr().as_ptr() as *mut {ty};
                        core::ptr::write(ptr, {ty}::new());
                        if let core::result::Result::Err(unpublished) = {store_tls_name}(ptr) {{
                            {reclaim_tls_name}(unpublished);
                            {tls_storage_failure_name}();
                        }}
                        ptr.as_ref().unwrap()
                    }}
                }}
            }}

            impl core::ops::DerefMut for {name} {{
                fn deref_mut(&mut self) -> &mut {ty} {{
                    extern crate alloc;
                    use alloc::alloc::Allocator;
                    unsafe {{
                        let mut ptr = {load_tls_name}();
                        if !ptr.is_null() {{
                            return ptr.as_mut().unwrap();
                        }}
                        let layout = alloc::alloc::Layout::new::<{ty}>();
                        let allocation = MetadataAllocator {{}}
                            .allocate(layout)
                            .unwrap_or_else(|_| alloc::alloc::handle_alloc_error(layout));
                        ptr = allocation.as_non_null_ptr().as_ptr() as *mut {ty};
                        core::ptr::write(ptr, {ty}::new());
                        if let core::result::Result::Err(unpublished) = {store_tls_name}(ptr) {{
                            {reclaim_tls_name}(unpublished);
                            {tls_storage_failure_name}();
                        }}
                        ptr.as_mut().unwrap()
                    }}
                }}
            }}
        ",
        ty = ty,
        name = name,
        func = func,
        tsd_state_name = tsd_state_name,
        ensure_tsd_name = ensure_tsd_name,
        reset_tsd_name = reset_tsd_name,
        load_tls_name = load_tls_name,
        store_tls_name = store_tls_name,
        reclaim_tls_name = reclaim_tls_name,
        tls_storage_failure_name = tls_storage_failure_name,
    );

    expanded
        .parse()
        .expect("Error parsing formatted string into token stream.")
}

fn gcd(a: usize, b: usize) -> usize {
    match ((a, b), (a & 1, b & 1)) {
        ((x, y), _) if x == y => y,
        ((0, x), _) | ((x, 0), _) => x,
        ((x, y), (0, 1)) | ((y, x), (1, 0)) => gcd(x >> 1, y),
        ((x, y), (0, 0)) => gcd(x >> 1, y >> 1) << 1,
        ((x, y), (1, 1)) => {
            let (x, y) = (core::cmp::min(x, y), core::cmp::max(x, y));
            gcd((y - x) >> 1, x)
        }
        _ => unreachable!(),
    }
}

fn lcm(a: usize, b: usize) -> usize {
    if a == 0 || b == 0 {
        return 0;
    }
    a / gcd(a, b) * b
}

const DEFAULT_TARGET_PAGE_SIZE: usize = 4096;
const MACOS_AARCH64_PAGE_SIZE: usize = 16 * 1024;
const RESOLVED_PAGE_SIZE_ENV: &str = "UNIALLOC_RESOLVED_PAGE_SIZE";
const TARGET_PAGE_SIZE_ENV: &str = "UNIALLOC_TARGET_PAGE_SIZE";

fn parse_power_of_two_usize(value: Option<String>) -> Option<usize> {
    value
        .and_then(|raw| raw.trim().parse::<usize>().ok())
        .filter(|page_size| *page_size != 0 && page_size.is_power_of_two())
}

fn default_page_size_for_target(target_os: &str, target_arch: &str) -> usize {
    match (target_os, target_arch) {
        ("macos", "aarch64") => MACOS_AARCH64_PAGE_SIZE,
        _ => DEFAULT_TARGET_PAGE_SIZE,
    }
}

fn page_size_for_generate_num_pages() -> usize {
    parse_power_of_two_usize(std::env::var(RESOLVED_PAGE_SIZE_ENV).ok())
        .or_else(|| parse_power_of_two_usize(std::env::var(TARGET_PAGE_SIZE_ENV).ok()))
        .unwrap_or_else(|| {
            let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
            let target_arch = std::env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_default();
            default_page_size_for_target(&target_os, &target_arch)
        })
}

fn num_pages_for_size_class(page_size: usize, size_class: usize) -> usize {
    if size_class == 0 {
        return 0;
    }
    lcm(page_size, size_class) / page_size
}

/// Calculates the number of target pages backing each size class.
pub fn generate_num_pages(input: TokenStream) -> TokenStream {
    let mut it = input.into_iter();
    let mut vals = String::new();
    let mut idx = 0;

    loop {
        let cl = match it.next() {
            Some(TokenTree::Literal(l)) => l.to_string(),
            Some(TokenTree::Punct(_)) => continue,
            Some(_) => panic!("Expect Literal, Punct, or end"),
            None => break,
        };

        let cl = cl.parse::<usize>().unwrap();
        // Keep this proc-macro's page geometry in lockstep with
        // `unialloc/build.rs`.  The build script exports the resolved value via
        // `UNIALLOC_RESOLVED_PAGE_SIZE`; direct macro-crate tests and unusual
        // compile flows fall back to `UNIALLOC_TARGET_PAGE_SIZE` or a target
        // default.  Hard-coding 4 KiB here made the dormant MiMalloc-style size
        // class table over-reserve pages on native Apple Silicon (16 KiB pages),
        // which is exactly the kind of hidden internal/RSS fragmentation this
        // allocator work is trying to remove.
        let page_size = page_size_for_generate_num_pages();
        let num_pages = num_pages_for_size_class(page_size, cl);

        vals.push_str(&format!("{}, ", num_pages));

        idx += 1;
    }

    format!(
        "
            const SIZE_CLASS_PAGES: [usize; {sz}] = [
                {vals}
            ];
        ",
        sz = idx,
        vals = vals,
    )
    .parse()
    .expect("Error parsing formatted string into token stream.")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generate_num_pages_prefers_resolved_page_size() {
        assert_eq!(
            parse_power_of_two_usize(Some("16384".to_string())),
            Some(16 * 1024)
        );
        assert_eq!(parse_power_of_two_usize(Some("12288".to_string())), None);
        assert_eq!(default_page_size_for_target("macos", "aarch64"), 16 * 1024);
        assert_eq!(default_page_size_for_target("linux", "x86_64"), 4096);
    }

    #[test]
    fn num_pages_for_size_class_uses_target_page_geometry() {
        assert_eq!(num_pages_for_size_class(4096, 40960), 10);
        assert_eq!(num_pages_for_size_class(16 * 1024, 40960), 5);
        assert_eq!(num_pages_for_size_class(16 * 1024, 0), 0);
    }
}
