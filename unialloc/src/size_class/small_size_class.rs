use crate::size_class::SizeClass;

/// Fixed-heap size classes used by constrained boot targets.
///
/// The original fixed-heap table rounded every non-tiny request to the next
/// power of two (`33 -> 64`, `1025 -> 2048`).  That kept metadata small, but it
/// caused avoidable internal fragmentation exactly on the memory-constrained
/// path.  The table below is deliberately *not* as dense as the production
/// tcmalloc-style classes: fixed-heap boot tests may have only a few dozen
/// pages, and each additional size class can pin its first slab.  These classes
/// cut the worst power-of-two rounding waste roughly in half while keeping the
/// number of active fixed-heap slabs small.
const SIZE_ARRAY: [u16; TOTAL_SIZE_CLASS] = [
    0_u16, 8_u16, 16_u16, 24_u16, 32_u16, 48_u16, 64_u16, 80_u16, 96_u16, 128_u16, 160_u16,
    192_u16, 256_u16, 384_u16, 512_u16, 768_u16, 1024_u16, 1536_u16, 2048_u16,
];

pub const BACKEND_MAX_PAGE: usize = 32;

pub const TOTAL_SIZE_CLASS: usize = 19;

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
    if req == 0 {
        return SizeClass::Base(0);
    }

    if req > MAX_SIZE {
        return SizeClass::Large(req);
    }

    let mut idx = 1;
    while idx < TOTAL_SIZE_CLASS {
        if req <= SIZE_ARRAY[idx] as usize {
            return SizeClass::Base(idx);
        }
        idx += 1;
    }
    SizeClass::Large(req)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_size_uses_internal_sentinel_class() {
        assert_eq!(get_size_class(0), SizeClass::Base(0));
        assert_eq!(get_rounded_size_by_idx(0), 0);
    }

    #[test]
    fn fixed_heap_classes_never_under_round_supported_requests() {
        for req in 1..=MAX_SIZE {
            let idx = match get_size_class(req) {
                SizeClass::Base(idx) => idx,
                SizeClass::Large(size) => panic!("unexpected large class for {}: {}", req, size),
            };
            assert!(
                idx < TOTAL_SIZE_CLASS,
                "class index out of range for {}",
                req
            );
            let rounded = get_rounded_size_by_idx(idx);
            assert!(
                rounded >= req,
                "size class {} rounded {} below request {}",
                idx,
                rounded,
                req
            );
            assert!(rounded <= MAX_SIZE, "rounded size exceeds fixed heap max");
        }
    }

    #[test]
    fn non_power_of_two_requests_round_up_to_safe_class() {
        let cases = [
            (1, 1, 8),
            (8, 1, 8),
            (9, 2, 16),
            (16, 2, 16),
            (17, 3, 24),
            (24, 3, 24),
            (33, 5, 48),
            (65, 7, 80),
            (1025, 17, 1536),
            (2048, 18, 2048),
        ];

        for (req, expected_idx, expected_size) in cases {
            assert_eq!(get_size_class(req), SizeClass::Base(expected_idx));
            assert_eq!(get_rounded_size_by_idx(expected_idx), expected_size);
        }
    }

    #[test]
    fn oversized_requests_remain_large_without_overflowing_rounding() {
        assert_eq!(get_size_class(MAX_SIZE + 1), SizeClass::Large(MAX_SIZE + 1));
        assert_eq!(get_size_class(usize::MAX), SizeClass::Large(usize::MAX));
    }

    #[test]
    fn fixed_heap_classes_bound_internal_fragmentation() {
        for req in 1..=MAX_SIZE {
            let idx = match get_size_class(req) {
                SizeClass::Base(idx) => idx,
                SizeClass::Large(size) => panic!("unexpected large class for {}: {}", req, size),
            };
            let rounded = get_rounded_size_by_idx(idx);
            let slack = rounded - req;
            assert!(
                slack <= core::cmp::max(8, req / 2),
                "request {} rounded to {} with excessive internal fragmentation",
                req,
                rounded
            );
        }
    }
}
