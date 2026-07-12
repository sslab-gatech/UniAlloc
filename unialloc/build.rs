use cfg_aliases::cfg_aliases;
use std::env;
use std::fs;
use std::path::Path;
use std::process::Command;

#[cfg(target_os = "linux")]
fn host_rseq_available() -> bool {
    unsafe {
        let rc = libc::syscall(334, 0, 0, 0, 0);
        if rc != -1 {
            return true;
        }

        matches!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::EINVAL)
        )
    }
}

#[cfg(not(target_os = "linux"))]
fn host_rseq_available() -> bool {
    false
}

fn env_usize(name: &str) -> Option<usize> {
    env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<usize>().ok())
        .filter(|value| *value != 0)
}

fn env_power_of_two_usize(name: &str) -> Option<usize> {
    env_usize(name).filter(|value| value.is_power_of_two())
}

fn target_page_size() -> usize {
    if let Some(page_size) = env_power_of_two_usize("UNIALLOC_TARGET_PAGE_SIZE") {
        return page_size;
    }

    let host = env::var("HOST").unwrap_or_default();
    let target = env::var("TARGET").unwrap_or_default();
    if !host.is_empty() && host == target {
        return page_size::get();
    }

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let target_arch = env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_default();
    match (target_os.as_str(), target_arch.as_str()) {
        // Windows exposes a separate 64 KiB allocation granularity, but the
        // allocator's PAGE_SIZE constant is the hardware page size used for
        // layout rounding and mprotect/VirtualProtect ranges.
        ("windows", _) => 4096,
        // Linux/Rust-for-Linux, Redox, and no_std fixed-heap smoke targets use
        // 4 KiB unless a target-specific page size is explicitly supplied.
        ("linux", _) | ("redox", _) | ("none", _) => 4096,
        // Native Apple Silicon processes use 16 KiB pages; x86_64 macOS keeps
        // 4 KiB.  For native builds above, page_size::get() remains the source
        // of truth.
        ("macos", "aarch64") => 16 * 1024,
        ("macos", _) => 4096,
        _ => page_size::get(),
    }
}

fn target_rseq_cfg_enabled() -> bool {
    env::var("CARGO_CFG_TARGET_OS").ok().as_deref() == Some("linux")
        && env::var("CARGO_CFG_TARGET_ARCH").ok().as_deref() == Some("x86_64")
        && env::var_os("CARGO_FEATURE_RSEQ").is_some()
        && env::var_os("CARGO_FEATURE_FIXED_HEAP").is_none()
}

fn target_has_rseq_runtime() -> bool {
    if !target_rseq_cfg_enabled() {
        return false;
    }
    let host = env::var("HOST").unwrap_or_default();
    let target = env::var("TARGET").unwrap_or_default();
    !host.is_empty() && host == target && host_rseq_available()
}

fn rustc_minor_version() -> Option<u32> {
    let rustc = env::var("RUSTC").unwrap_or_else(|_| "rustc".to_string());
    let output = Command::new(rustc).arg("--version").output().ok()?;
    let text = String::from_utf8(output.stdout).ok()?;
    // Expected shape: `rustc 1.98.0-nightly (...)`.
    text.split_whitespace()
        .nth(1)?
        .split('.')
        .nth(1)?
        .parse::<u32>()
        .ok()
}

fn rustc_minor_at_least(rustc_minor: Option<u32>, stabilized_minor: u32) -> bool {
    rustc_minor.map_or(false, |minor| minor >= stabilized_minor)
}

fn emit_feature_stability_cfg(rustc_minor: Option<u32>, cfg_name: &str, stabilized_minor: u32) {
    if rustc_minor_at_least(rustc_minor, stabilized_minor) {
        println!("cargo:rustc-cfg={}", cfg_name);
    }
}

static IDX_BIT: usize = 32;

fn calculate_val(pg: usize, tar: usize) -> (usize, usize, usize) {
    let mut num_in_pg = pg / tar;
    if num_in_pg > IDX_BIT {
        ((num_in_pg + IDX_BIT - 1) / IDX_BIT, tar, 1)
    } else {
        let mut ratio = 1_usize;
        let mut pa_size = pg;
        while ratio < 8 && num_in_pg <= 32 {
            ratio += 1;
            pa_size <<= 1;
            num_in_pg = pa_size / tar;
        }
        pa_size >>= 1;
        num_in_pg = pa_size / tar;
        ((num_in_pg + IDX_BIT - 1) / IDX_BIT, tar, pa_size / pg)
    }
}

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-env-changed=UNIALLOC_TARGET_PAGE_SIZE");
    println!("cargo:rerun-if-env-changed=UNIALLOC_TARGET_NCPU");
    let rustc_minor = rustc_minor_version();
    // `cargo:rustc-check-cfg` is useful on current toolchains but noisy on the
    // paper-pinned 2022 nightly, where Cargo asks for the old experimental
    // `-Zcheck-cfg=output` flag.  Emit the directive only for newer toolchains
    // that understand it natively; the cfgs themselves are still set below.
    if rustc_minor.map_or(false, |minor| minor >= 80) {
        println!("cargo:rustc-check-cfg=cfg(rseq)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_btree_extract_if_range)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_has_stable_alloc_layout_extra)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_has_stable_alloc_c_string)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_has_stable_asm_const)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_has_stable_const_mut_refs)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_has_stable_map_first_last)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_has_stable_nonnull_slice_from_raw_parts)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_has_stable_raw_ref_op)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_has_stable_slice_ptr_len)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_addr_of_mut_static_mut_is_safe)");
        println!("cargo:rustc-check-cfg=cfg(unialloc_target_arm64e)");
    }

    // Keep the paper-pinned 2022 nightly on the feature gates it still needs,
    // but avoid enabling gates that current rustc has stabilized.  This makes
    // `cargo +nightly check` useful signal instead of expected-version noise.
    emit_feature_stability_cfg(rustc_minor, "unialloc_has_stable_map_first_last", 66);
    // `alloc::ffi::CString` was still feature-gated on the paper-pinned
    // pre-release 1.64 nightly even though 1.64 stable later exposed it. Use
    // 1.65 as the first whole-minor boundary where the gate is unnecessary.
    emit_feature_stability_cfg(rustc_minor, "unialloc_has_stable_alloc_c_string", 65);
    emit_feature_stability_cfg(
        rustc_minor,
        "unialloc_has_stable_nonnull_slice_from_raw_parts",
        70,
    );
    emit_feature_stability_cfg(rustc_minor, "unialloc_has_stable_slice_ptr_len", 79);
    emit_feature_stability_cfg(rustc_minor, "unialloc_has_stable_raw_ref_op", 82);
    emit_feature_stability_cfg(rustc_minor, "unialloc_has_stable_asm_const", 82);
    emit_feature_stability_cfg(rustc_minor, "unialloc_has_stable_const_mut_refs", 83);
    emit_feature_stability_cfg(rustc_minor, "unialloc_has_stable_alloc_layout_extra", 95);
    // Newer rustc treats `addr_of_mut!(STATIC_MUT)` as a raw-address operation
    // that no longer needs an outer unsafe block; the 2022 paper pin still
    // requires it.  Keep both builds warning-clean without weakening the fixed-
    // heap lock boundary in `collections::radix_tree`.
    if rustc_minor_at_least(rustc_minor, 98) {
        println!("cargo:rustc-cfg=unialloc_addr_of_mut_static_mut_is_safe");
    }

    // Current rustc exposes `arm64e-apple-darwin` as a target but its Mach-O
    // TLV descriptors fault before allocator code when a no_std runtime probe
    // touches Rust `#[thread_local]` storage.  Emit a target-specific cfg so
    // UniAlloc can keep the fast compiler TLS path on normal Apple Silicon
    // builds while using its existing pthread-key destructor path for arm64e.
    if env::var("TARGET").ok().as_deref() == Some("arm64e-apple-darwin") {
        println!("cargo:rustc-cfg=unialloc_target_arm64e");
    }

    // Rust's BTree drain API changed between the paper-pinned 2022 nightly and
    // current nightlies.  Keep old builds on `drain_filter`, and let newer
    // builds use its range-aware successor `extract_if(.., pred)` while
    // preserving the benchmark's remove-matching-elements semantics.
    if rustc_minor.map_or(false, |minor| minor >= 90) {
        println!("cargo:rustc-cfg=unialloc_btree_extract_if_range");
    }

    // Setup cfg aliases
    cfg_aliases! {
        // Platforms
        x64: { target_arch = "x86_64"},
        aarch64: { target_arch = "aarch64"},
        mac_aarch64: { all(target_os = "macos", target_arch = "aarch64") },
        mac_x64: { all(target_os = "macos", target_arch = "x86_64") },
        linux_aarch64: { all(target_os = "linux", target_arch = "aarch64") },
        linux_x64: { all(target_os = "linux", target_arch = "x86_64") },
        macos: { target_os = "macos" },
        linux: { target_os = "linux" },
        // The in-crate rseq fast path contains x86_64-specific inline
        // assembly.  Other Linux architectures must use the conservative
        // fallback instead of compiling the x86 transaction sequence.
        rseq: { all(target_os = "linux", target_arch = "x86_64", feature = "rseq", not(feature = "fixed_heap")) },
    }

    let out_dir = env::var_os("OUT_DIR").unwrap();
    let dest_path = Path::new(&out_dir).join("consts.rs");
    let page_size = target_page_size();
    // Proc macros run while rustc compiles this crate, so propagate the exact
    // page geometry resolved here.  `generate_num_pages!` uses the same value as
    // the generated `PAGE_SIZE` constant instead of silently assuming 4 KiB.
    println!("cargo:rustc-env=UNIALLOC_RESOLVED_PAGE_SIZE={}", page_size);
    let ncpu = env_usize("UNIALLOC_TARGET_NCPU").unwrap_or_else(num_cpus::get);
    let has_rseq = target_has_rseq_runtime();

    let content = format!(
        "pub const NCPU: usize = {};\n
         pub const PAGE_SIZE: usize = {};\n
         pub const HAS_RSEQ: bool = {};\n\n",
        ncpu, page_size, has_rseq
    );

    generate_sizeclass(page_size);

    fs::write(&dest_path, content).unwrap();
}

static SIZE_ARRAY: [usize; 63] = [
    8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144, 160, 176, 192, 208,
    224, 240, 256, 280, 304, 352, 384, 424, 480, 512, 576, 640, 704, 832, 896, 1024, 1152, 1280,
    1408, 1536, 1792, 2048, 2176, 2304, 2432, 2944, 3200, 3584, 4096, 4608, 5376, 6528, 8192, 9344,
    10880, 13056, 13952, 16384, 19072, 21760, 24576, 28032,
];

fn generate_sizeclass(page_size: usize) {
    let out_dir = env::var_os("OUT_DIR").unwrap();
    let dest_path = Path::new(&out_dir).join("sizeclass_consts.rs");
    let mut content = format!("const IDX_NP: [u16;{}] = [ ", SIZE_ARRAY.len());
    let mut offset_arr = format!("const OFFSET_ARRAY: [u16;{}] = [ ", SIZE_ARRAY.len());
    let mut offset_limit = format!("const OFFSET_LIMIT: [u16;{}] = [ ", SIZE_ARRAY.len());
    let mut start = SIZE_ARRAY.len().next_power_of_two() / 4;
    for (idx, val) in SIZE_ARRAY.iter().enumerate() {
        let template: (usize, usize, usize) = calculate_val(page_size, *val);
        let num = template.2 * page_size / *val;

        content.push_str(&format!("{}", num));
        offset_arr.push_str(&format!("{}", start));
        if num.is_power_of_two() {
            start += 2 * num;
        } else {
            start += 2 * num.next_power_of_two();
        }

        offset_limit.push_str(&format!("{}", start));
        if idx != SIZE_ARRAY.len() - 1 {
            content.push_str(", ");
            offset_arr.push_str(", ");
            offset_limit.push_str(", ");
        }
    }
    assert!(start * 8 <= 14 * page_size);
    content.push_str("];\n");
    offset_arr.push_str("];\n");
    offset_limit.push_str("];\n");
    content.push_str(&offset_arr);
    content.push_str(&offset_limit);
    fs::write(&dest_path, content).unwrap();
}
