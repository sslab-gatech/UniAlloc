use crate::size_class::SizeClass;

const SIZE_ARRAY: [u16; 10] = [
    0_u16, 8_u16, 16_u16, 32_u16, 64_u16, 128_u16, 256_u16, 512_u16, 1024_u16, 2048_u16,
];

pub const BACKEND_MAX_PAGE: usize = 32;

pub const TOTAL_SIZE_CLASS: usize = 10;

pub const MAX_SIZE: usize = 2048;

pub fn get_rounded_size_by_idx(idx: usize) -> usize {
    SIZE_ARRAY[idx] as usize
}

pub fn get_size_class_by_idx(idx: usize) -> usize {
    SIZE_ARRAY[idx] as usize
}

pub fn get_num_pages_by_idx(_idx: usize) -> usize {
    1
}

pub fn get_size_class(req: usize) -> SizeClass {
    let final_size = (req + 7) & !(7_usize);
    if final_size <= MAX_SIZE {
        let idx = final_size.trailing_zeros() as usize;
        SizeClass::Base(idx - 2)
    } else {
        SizeClass::Large(req)
    }
}
