#[cfg(all(target_arch = "x86_64", target_os = "linux"))]
mod x64_linux {
    pub const PAGE_SIZE: usize = 0x1000;

    // 1  GB -- 19 (0x40000000 bytes)
    // 2  GB -- 20
    // 4  Gb -- 21
    // 8  GB -- 22
    // 16 GB -- 23
    pub const LEVEL_COUNT: u8 = 23;

    // 2 ** MAX_ORDER will be the maximum chunk size that can be allocated
    // pub const MAX_ORDER: u8 = LEVEL_COUNT - 1;

    // 2 ** 12 = 4096
    // 2 ** 12 will be the minimum chunk size that can be allocated
    // pub const BASE_ORDER: u8 = 12;

    // pub const MAX_ORDER_SIZE: u8 = BASE_ORDER + MAX_ORDER;
}

#[cfg(all(target_arch = "aarch64", target_os = "macos"))]
mod aarch64_macos {
    pub const PAGE_SIZE: usize = 0x4000;
    pub const LEVEL_COUNT: u8 = 23;
    // pub const MAX_ORDER: u8 = LEVEL_COUNT - 1;
    // pub const BASE_ORDER: u8 = 14;
    // pub const MAX_ORDER_SIZE: u8 = BASE_ORDER + MAX_ORDER
}

#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
mod x64_window {
    pub const PAGE_SIZE: usize = 0x1000;

    // 1  GB -- 19 (0x40000000 bytes)
    // 2  GB -- 20
    // 4  Gb -- 21
    // 8  GB -- 22
    // 16 GB -- 23
    pub const LEVEL_COUNT: u8 = 23;

    // 2 ** MAX_ORDER will be the maximum chunk size that can be allocated
    // pub const MAX_ORDER: u8 = LEVEL_COUNT - 1;

    // 2 ** 12 = 4096
    // 2 ** 12 will be the minimum chunk size that can be allocated
    // pub const BASE_ORDER: u8 = 12;

    // pub const MAX_ORDER_SIZE: u8 = BASE_ORDER + MAX_ORDER;
}

#[cfg(all(target_arch = "aarch64", target_os = "macos"))]
pub use aarch64_macos::*;
#[cfg(all(target_arch = "x86_64", target_os = "linux"))]
pub use x64_linux::*;
#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
pub use x64_window::*;
