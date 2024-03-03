use cfg_aliases::cfg_aliases;
use std::env;
use std::fs;
use std::path::Path;

#[cfg(rseq)]
fn rseq_available() -> bool {
    unsafe {
        let rc = libc::syscall(334, 0, 0, 0, 0);
        if rc != -1 {
            panic!("WTF! (rc = {})", rc);
        }

        match *libc::__errno_location() {
            libc::ENOSYS => false,
            libc::EINVAL => true,
            num => {
                panic!("WTF? {}", num)
            }
        }
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
        rseq: { all(target_os = "linux", feature = "rseq") },
    }

    let out_dir = env::var_os("OUT_DIR").unwrap();
    let dest_path = Path::new(&out_dir).join("consts.rs");

    #[cfg(not(rseq))]
    let has_rseq = false;

    #[cfg(rseq)]
    let has_rseq = rseq_available();

    let content = format!(
        "pub const NCPU: usize = {};\n
         pub const PAGE_SIZE: usize = {};\n
         pub const HAS_RSEQ: bool = {};\n\n",
        num_cpus::get(),
        page_size::get(),
        has_rseq
    );

    generate_sizeclass();

    fs::write(&dest_path, content).unwrap();
}

static SIZE_ARRAY: [usize; 63] = [
    8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144, 160, 176, 192, 208,
    224, 240, 256, 280, 304, 352, 384, 424, 480, 512, 576, 640, 704, 832, 896, 1024, 1152, 1280,
    1408, 1536, 1792, 2048, 2176, 2304, 2432, 2944, 3200, 3584, 4096, 4608, 5376, 6528, 8192, 9344,
    10880, 13056, 13952, 16384, 19072, 21760, 24576, 28032,
];

fn generate_sizeclass() {
    let out_dir = env::var_os("OUT_DIR").unwrap();
    let dest_path = Path::new(&out_dir).join("sizeclass_consts.rs");
    let mut content = format!("const IDX_NP: [u16;{}] = [ ", SIZE_ARRAY.len());
    let mut offset_arr = format!("const OFFSET_ARRAY: [u16;{}] = [ ", SIZE_ARRAY.len());
    let mut offset_limit = format!("const OFFSET_LIMIT: [u16;{}] = [ ", SIZE_ARRAY.len());
    let mut start = SIZE_ARRAY.len().next_power_of_two() / 4;
    for (idx, val) in SIZE_ARRAY.iter().enumerate() {
        let template: (usize, usize, usize) = calculate_val(page_size::get(), *val);
        let num = template.2 * page_size::get() / *val;

        content.push_str(&*format!("{}", num));
        offset_arr.push_str(&*format!("{}", start));
        if num.is_power_of_two() {
            start += 2 * num;
        } else {
            start += 2 * num.next_power_of_two();
        }

        offset_limit.push_str(&*format!("{}", start));
        if idx != SIZE_ARRAY.len() - 1 {
            content.push_str(", ");
            offset_arr.push_str(", ");
            offset_limit.push_str(", ");
        }
    }
    assert!(start * 8 <= 14 * page_size::get());
    content.push_str("];\n");
    offset_arr.push_str("];\n");
    offset_limit.push_str("];\n");
    content.push_str(&*offset_arr);
    content.push_str(&*offset_limit);
    fs::write(&dest_path, content).unwrap();
}
