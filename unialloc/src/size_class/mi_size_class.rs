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

const SIZE_ARRAY: [usize; TOTAL_SIZE_CLASS] = [
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512,
    640, 768, 896, 1024, 1280, 1536, 1792, 2048, 2560, 3072, 3584, 4096, 5120, 6144, 7168, 8192,
    10240, 12288, 14336, 16384, 20480, 24576, 28672, 32768, 40960,
];

/// Resolve a supported request size to its base size-class index.
///
/// `word_count` is the request rounded up to machine words.  Tiny classes map
/// one word per class (`1..=8`) and larger classes use mimalloc's four-classes
/// per size decade formula.  Keeping this in one helper avoids the old split
/// logic where tests used `get_idx_by_size` and the allocator hot path carried
/// a copy of the formula with slightly different edge handling.
#[inline]
fn base_idx_from_word_count(word_count: usize) -> usize {
    if word_count <= 8 {
        return word_count;
    }

    let n = word_count - 1;
    let b = bsr(n as u64);
    (((b << 2) + ((n >> (b - 2)) & 0x03) as u8) - 3) as usize
}

fn get_idx_by_size(req: usize) -> usize {
    base_idx_from_word_count(get_num_of_usize(req))
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
    if req == 0 {
        return SizeClass::Base(0);
    }

    if req > MAX_SIZE {
        return SizeClass::Large(req);
    }

    SizeClass::Base(base_idx_from_word_count(get_num_of_usize(req)))
}

pub fn get_rounded_size_by_idx(idx: usize) -> usize {
    SIZE_ARRAY[idx]
}

generate_num_pages! {
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512,
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
        assert_eq!(get_idx_by_size(0), 0);
        assert_eq!(get_idx_by_size(1), 1);
        assert_eq!(get_idx_by_size(8), 1);
        assert_eq!(get_idx_by_size(9), 2);
        assert_eq!(get_idx_by_size(64), 8);
        assert_eq!(get_idx_by_size(80), 9);
        assert_eq!(get_idx_by_size(81), 10);
        assert_eq!(get_idx_by_size(40960), 45);
    }

    #[test]
    fn helper_and_public_size_class_match_for_supported_requests() {
        for req in 0..=MAX_SIZE {
            assert_eq!(
                get_size_class(req).index(),
                get_idx_by_size(req),
                "request {} should use the same size-class resolver in tests and allocator code",
                req
            );
        }
    }

    #[test]
    fn zero_size_uses_internal_sentinel_class() {
        assert_eq!(get_size_class(0), SizeClass::Base(0));
        assert_eq!(get_idx_by_size(0), 0);
        assert_eq!(get_rounded_size_by_idx(0), 0);
        assert_eq!(get_num_pages_by_idx(0), 0);
    }

    #[test]
    fn generated_page_counts_follow_resolved_crate_page_size() {
        const fn gcd_const(mut a: usize, mut b: usize) -> usize {
            while b != 0 {
                let r = a % b;
                a = b;
                b = r;
            }
            a
        }

        const fn expected_pages(page_size: usize, size_class: usize) -> usize {
            if size_class == 0 {
                0
            } else {
                (page_size / gcd_const(page_size, size_class)) * size_class / page_size
            }
        }

        let samples = [0usize, 4096, 8192, 10240, 16384, 24576, 40960];
        for size in samples {
            let idx = get_size_class(size).index();
            assert_eq!(get_rounded_size_by_idx(idx), size);
            assert_eq!(
                get_num_pages_by_idx(idx),
                expected_pages(crate::PAGE_SIZE, size),
                "SIZE_CLASS_PAGES must use the same PAGE_SIZE as build.rs"
            );
        }
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
