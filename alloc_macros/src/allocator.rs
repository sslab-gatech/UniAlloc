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
// // use customs allocator
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
// The internal representation requires `Box` to allocate objects on heap.
// However, large objects can potentially overflows the stack.
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

    let expanded = format!(
        "
            // The `VALUE` needs to be declared at the outside of [`deref`]
            // and [`deref_mut`]
            #[thread_local]
            static mut {name}_VALUE: * mut {ty} =core::ptr::null_mut();
            static mut TSD_INITIALIZED: bool = false;

            // ZST for dereference
            pub struct {name};

            impl core::ops::Deref for {name} {{
                type Target = {ty};

                fn deref(&self) -> &{ty} {{
                    extern crate alloc;
                    use alloc::alloc::GlobalAlloc;
                    unsafe {{
                        if !{name}_VALUE.is_null() {{
                            return  {name}_VALUE.as_ref().unwrap();
                        }}
                        // let layout = Layout::new::<{ty}>();
                        // {name}_VALUE = META_BUMP.alloc(layout.size()).expect(\"err\")  as *mut {ty};
                        // core::ptr::write({name}_VALUE, {ty}::new());
                        let boxed_ptr:Box<{ty}, MetadataAllocator> =
                            Box::new_in(
                                {ty}::new()
                                , MetadataAllocator  {{ }});
                        {name}_VALUE = Box::into_raw(boxed_ptr);
                        if !TSD_INITIALIZED {{
                            register_tls_key({func});
                            TSD_INITIALIZED = true;
                        }}
                        save_tls({name}_VALUE as *mut u8);
                        {name}_VALUE.as_ref().unwrap()
                    }}
                }}
            }}

            impl core::ops::DerefMut for {name} {{
                fn deref_mut(&mut self) -> &mut {ty} {{
                    extern crate alloc;
                    use alloc::alloc::GlobalAlloc;
                    unsafe {{
                        if !{name}_VALUE.is_null() {{
                            return {name}_VALUE.as_mut().unwrap();
                        }}
                        let boxed_ptr:Box<{ty}, MetadataAllocator> =
                            Box::new_in(
                                {ty}::new()
                                , MetadataAllocator {{}}
                                );
                        {name}_VALUE = Box::into_raw(boxed_ptr);
                        if !TSD_INITIALIZED {{
                            register_tls_key({func});
                            TSD_INITIALIZED = true;
                        }}
                        save_tls({name}_VALUE as *mut u8);
                        {name}_VALUE.as_mut().unwrap()
                    }}
                }}
            }}
        ",
        ty = ty,
        name = name,
        func = func
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
    a * b / gcd(a, b)
}

/// Calculates the gcd of
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
        // #[cfg(target_arch = "x86_64")]
        // TODO: check how to determine page size dynamically
        const PAGE_SIZE: usize = 0x1000;
        // #[cfg(target_arch = "aarch64")]
        // let page_size = 0x10000;

        let num_pages = lcm(PAGE_SIZE, cl) / PAGE_SIZE;

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
