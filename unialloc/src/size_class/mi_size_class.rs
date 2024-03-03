use super::*;
use crate::*;
use alloc_macros::generate_num_pages;

const SZOFUSIZE: usize = core::mem::size_of::<usize>();
pub const TOTAL_SIZE_CLASS: usize = 46;
pub const MAX_SIZE: usize = 40960;
pub const BACKEND_MAX_PAGE: usize = 128;
fn bsr(x: u64) -> u8 {
    (63 - x.leading_zeros()) as u8
}

// TODO: handle size 0
const SIZE_ARRAY: [usize; TOTAL_SIZE_CLASS] = [
    8, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512,
    640, 768, 896, 1024, 1280, 1536, 1792, 2048, 2560, 3072, 3584, 4096, 5120, 6144, 7168, 8192,
    10240, 12288, 14336, 16384, 20480, 24576, 28672, 32768, 40960,
];

fn get_idx_by_size(req: usize) -> usize {
    let n: usize = get_num_of_usize(req) - 1;
    let b = bsr(n as u64);
    let idx = ((b << 2) + ((n >> (b - 2)) & 0x03) as u8) - 3;
    idx as usize
}

const fn get_num_of_usize(req: usize) -> usize {
    if req > (usize::MAX - SZOFUSIZE) {
        return req / SZOFUSIZE;
    }

    if req % SZOFUSIZE == 0 {
        req / SZOFUSIZE
    } else {
        let res = (req + SZOFUSIZE - 1) & !(SZOFUSIZE - 1);
        res / SZOFUSIZE
    }
}

pub fn get_num_pages_by_idx(idx: usize) -> usize {
    SIZE_CLASS_PAGES[idx]
}

pub fn get_size_class(req: usize) -> SizeClass {
    if req > MAX_SIZE {
        return SizeClass::Large(req);
    }

    let n: usize = get_num_of_usize(req);

    if n <= 8 {
        return SizeClass::Base(n);
    }

    let n = n - 1;

    let b = bsr(n as u64);
    let idx = ((b << 2) + ((n >> (b - 2)) & 0x03) as u8) - 3;
    SizeClass::Base(idx as usize)
}

pub fn get_rounded_size_by_idx(idx: usize) -> usize {
    SIZE_ARRAY[idx]
}

generate_num_pages! {
    8, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512,
    640, 768, 896, 1024, 1280, 1536, 1792, 2048, 2560, 3072, 3584, 4096, 5120, 6144, 7168, 8192,
    10240, 12288, 14336, 16384, 20480, 24576, 28672, 32768, 40960
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bsr_test() {
        assert_eq!(bsr(0b111000), 5);
    }

    #[test]
    fn get_size_class_sanity_check() {
        assert_eq!(get_size_class(0).index(), 0);
        assert_eq!(get_size_class(1).index(), 1);
        assert_eq!(get_size_class(2).index(), 1);
        assert_eq!(get_size_class(80), SizeClass::Base(9));
    }

    #[test]
    fn size_class_idx_sanity_check() {
        assert_eq!(get_idx_by_size(80), 9);
        assert_eq!(get_idx_by_size(81), 10);
        assert_eq!(get_idx_by_size(40960), 45);
    }

    #[test]
    fn size_greater_than64() {
        let targets = [
            64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512, 640, 768, 896, 1024,
            1280, 1536, 1792, 2048, 2560, 3072, 3584, 4096, 5120, 6144, 7168, 8192, 10240, 12288,
            14336, 16384, 20480, 24576, 28672, 32768, 40960,
        ];

        let mut start_idx = 8;
        for i in targets.iter() {
            assert_eq!(get_idx_by_size(*i), start_idx);
            start_idx += 1;
        }
    }

    #[test]
    fn num_of_uszie() {
        assert_eq!(get_num_of_usize(64), 8);
        assert_eq!(get_num_of_usize(9), 2);
        assert_eq!(get_num_of_usize(8), 1);
        assert_eq!(get_num_of_usize(0), 0);
        assert_eq!(get_num_of_usize(1), 1);
    }

    #[test]
    fn size_class() {
        assert_eq!(get_size_class(64).index(), get_idx_by_size(64));
        assert_eq!(get_size_class(65).index(), get_idx_by_size(65));
        assert_eq!(get_size_class(40960).index(), get_idx_by_size(40960));
        assert_eq!(get_size_class(32768).index(), get_idx_by_size(32768));
    }
}
