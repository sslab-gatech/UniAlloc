use super::SizeClass;
use crate::*;
use core::intrinsics::{likely, unlikely};

pub const TOTAL_SIZE_CLASS: usize = 64;
pub(crate) const NUM_SIZE_CLASSES: usize = TOTAL_SIZE_CLASS - 1;
pub const MAX_SIZE: usize = 28032;
const TINY_BATCH_SPAN_BYTES: usize = 8 * 1024;
const SMALL_BATCH_SPAN_BYTES: usize = 16 * 1024;

/// Returns index and size_class.
///
/// The allocator hot path uses the compact `IDX_ARRAY` lookup below instead of
/// the older binary-search/match tables.  Keep a single size table so geometry,
/// rounding, and tests cannot drift apart.
#[inline]
pub fn get_index_and_size(req_size: usize) -> (SizeClass, usize) {
    get_sizeclass_tuple2(req_size)
}

#[inline]
pub fn get_size_from_idx(idx: usize) -> usize {
    SIZE_CLASSES[idx] as usize
}

pub fn get_size_class(req: usize) -> SizeClass {
    get_sizeclass_tuple2(req).0
}
const IDX_ARRAY: [u8; ((MAX_SIZE + 127 + (120 << 7)) >> 7) + 1] = [
    0_u8, 1_u8, 2_u8, 3_u8, 4_u8, 5_u8, 6_u8, 7_u8, 8_u8, 9_u8, 10_u8, 11_u8, 12_u8, 13_u8, 14_u8,
    15_u8, 16_u8, 17_u8, 17_u8, 18_u8, 18_u8, 19_u8, 19_u8, 20_u8, 20_u8, 21_u8, 21_u8, 22_u8,
    22_u8, 23_u8, 23_u8, 24_u8, 24_u8, 25_u8, 25_u8, 25_u8, 26_u8, 26_u8, 26_u8, 27_u8, 27_u8,
    27_u8, 27_u8, 27_u8, 27_u8, 28_u8, 28_u8, 28_u8, 28_u8, 29_u8, 29_u8, 29_u8, 29_u8, 29_u8,
    30_u8, 30_u8, 30_u8, 30_u8, 30_u8, 30_u8, 30_u8, 31_u8, 31_u8, 31_u8, 31_u8, 32_u8, 32_u8,
    32_u8, 32_u8, 32_u8, 32_u8, 32_u8, 32_u8, 33_u8, 33_u8, 33_u8, 33_u8, 33_u8, 33_u8, 33_u8,
    33_u8, 34_u8, 34_u8, 34_u8, 34_u8, 34_u8, 34_u8, 34_u8, 34_u8, 35_u8, 35_u8, 35_u8, 35_u8,
    35_u8, 35_u8, 35_u8, 35_u8, 35_u8, 35_u8, 35_u8, 35_u8, 35_u8, 35_u8, 35_u8, 35_u8, 36_u8,
    36_u8, 36_u8, 36_u8, 36_u8, 36_u8, 36_u8, 36_u8, 37_u8, 37_u8, 37_u8, 37_u8, 37_u8, 37_u8,
    37_u8, 37_u8, 37_u8, 37_u8, 37_u8, 37_u8, 37_u8, 37_u8, 37_u8, 37_u8, 38_u8, 39_u8, 40_u8,
    41_u8, 42_u8, 42_u8, 43_u8, 43_u8, 44_u8, 45_u8, 46_u8, 47_u8, 47_u8, 47_u8, 47_u8, 48_u8,
    48_u8, 49_u8, 49_u8, 49_u8, 50_u8, 50_u8, 50_u8, 50_u8, 51_u8, 51_u8, 51_u8, 51_u8, 52_u8,
    52_u8, 52_u8, 52_u8, 52_u8, 52_u8, 53_u8, 53_u8, 53_u8, 53_u8, 53_u8, 53_u8, 53_u8, 53_u8,
    53_u8, 54_u8, 54_u8, 54_u8, 54_u8, 54_u8, 54_u8, 54_u8, 54_u8, 54_u8, 54_u8, 54_u8, 54_u8,
    54_u8, 55_u8, 55_u8, 55_u8, 55_u8, 55_u8, 55_u8, 55_u8, 55_u8, 55_u8, 56_u8, 56_u8, 56_u8,
    56_u8, 56_u8, 56_u8, 56_u8, 56_u8, 56_u8, 56_u8, 56_u8, 56_u8, 57_u8, 57_u8, 57_u8, 57_u8,
    57_u8, 57_u8, 57_u8, 57_u8, 57_u8, 57_u8, 57_u8, 57_u8, 57_u8, 57_u8, 57_u8, 57_u8, 57_u8,
    58_u8, 58_u8, 58_u8, 58_u8, 58_u8, 58_u8, 58_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8,
    59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8, 59_u8,
    60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8,
    60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 60_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8,
    61_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8, 61_u8,
    61_u8, 61_u8, 61_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8,
    62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 62_u8, 63_u8,
    63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8,
    63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8, 63_u8,
];

const SIZE_CLASSES: [u16; TOTAL_SIZE_CLASS] = [
    0_u16, 8_u16, 16_u16, 24_u16, 32_u16, 40_u16, 48_u16, 56_u16, 64_u16, 72_u16, 80_u16, 88_u16,
    96_u16, 104_u16, 112_u16, 120_u16, 128_u16, 144_u16, 160_u16, 176_u16, 192_u16, 208_u16,
    224_u16, 240_u16, 256_u16, 280_u16, 304_u16, 352_u16, 384_u16, 424_u16, 480_u16, 512_u16,
    576_u16, 640_u16, 704_u16, 832_u16, 896_u16, 1024_u16, 1152_u16, 1280_u16, 1408_u16, 1536_u16,
    1792_u16, 2048_u16, 2176_u16, 2304_u16, 2432_u16, 2944_u16, 3200_u16, 3584_u16, 4096_u16,
    4608_u16, 5376_u16, 6528_u16, 8192_u16, 9344_u16, 10880_u16, 13056_u16, 13952_u16, 16384_u16,
    19072_u16, 21760_u16, 24576_u16, 28032_u16,
];

#[inline]
pub(crate) fn get_sizeclass_tuple2(req_size: usize) -> (SizeClass, usize) {
    if likely(req_size <= 1024) {
        let idx = (req_size + 7) >> 3;
        let new_size_idx = IDX_ARRAY[idx] as usize;
        let new_size = SIZE_CLASSES[new_size_idx] as usize;
        (SizeClass::Base(new_size_idx), new_size)
    } else if req_size <= MAX_SIZE {
        let idx = (req_size + 127 + (120 << 7)) >> 7;
        let new_size_idx = IDX_ARRAY[idx] as usize;
        let new_size = SIZE_CLASSES[new_size_idx] as usize;
        (SizeClass::Base(new_size_idx), new_size)
    } else {
        (SizeClass::Large(req_size), req_size)
    }
}

// Below are the stable size-class APIs used by the allocator.
pub const BACKEND_MAX_PAGE: usize = 128;

pub fn get_rounded_size_by_idx(idx: usize) -> usize {
    SIZE_CLASSES[idx] as usize
}

pub fn get_size_class_by_idx(idx: usize) -> usize {
    SIZE_CLASSES[idx] as usize
}

pub fn get_num_pages_by_idx(idx: usize) -> usize {
    size_class_span_pages(SIZE_CLASSES[idx] as usize)
}

const fn ceil_div_usize(value: usize, divisor: usize) -> usize {
    if value == 0 {
        0
    } else {
        1 + ((value - 1) / divisor)
    }
}

#[inline]
fn target_pages_for_span_bytes(bytes: usize) -> usize {
    ceil_div_usize(bytes, crate::PAGE_SIZE)
}

#[inline]
fn size_class_span_pages(rounded_size: usize) -> usize {
    match rounded_size {
        0 => 0,
        // Tiny classes should batch many objects per slab; one target page is
        // enough and avoids a fixed 4KiB-page table on 16KiB-page platforms.
        1..=240 => 1,
        // Mid-tiny classes get an ~8KiB batch, rounded to the active target page
        // size.  This preserves reuse density on 4KiB targets without pinning
        // extra pages on Apple Silicon's 16KiB pages.
        241..=480 => {
            target_pages_for_span_bytes(core::cmp::max(rounded_size, TINY_BATCH_SPAN_BYTES))
        }
        // Small/medium classes get an ~16KiB batch so slab handoff amortizes
        // metadata work while still bounding one-live-object RSS pressure.
        481..=8192 => {
            target_pages_for_span_bytes(core::cmp::max(rounded_size, SMALL_BATCH_SPAN_BYTES))
        }
        // Larger size classes use the minimum whole-page one-slot span.  This
        // intentionally trades some large-class batching for lower external
        // fragmentation and smaller retained-empty-slab footprint.
        _ => target_pages_for_span_bytes(rounded_size),
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn size_class_page_geometry_is_valid_for_every_base_class() {
        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded = get_rounded_size_by_idx(idx);
            let pages = get_num_pages_by_idx(idx);
            assert!(pages > 0, "class {} must have a non-zero span", idx);
            let span = pages
                .checked_mul(crate::PAGE_SIZE)
                .expect("test span must not overflow");
            assert!(
                span >= rounded,
                "class {} rounded size {} must fit in {} pages ({} bytes)",
                idx,
                rounded,
                pages,
                span
            );
            let (slot_count, stride) = crate::sc::checked_size_class_geometry(rounded, pages)
                .unwrap_or_else(|| {
                    panic!(
                        "class {} ({} bytes, {} pages) has invalid geometry",
                        idx, rounded, pages
                    )
                });
            assert!(
                slot_count > 0,
                "class {} must expose at least one slot",
                idx
            );
            assert!(
                stride >= rounded,
                "class {} stride must not under-round",
                idx
            );
            assert!(
                slot_count * stride <= span,
                "class {} object slots must fit inside the span",
                idx
            );
        }
    }

    #[test]
    fn medium_and_large_class_spans_are_footprint_bounded() {
        let max_span = core::cmp::max(32 * 1024, crate::PAGE_SIZE);
        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded = get_rounded_size_by_idx(idx);
            let pages = get_num_pages_by_idx(idx);
            let span = pages * crate::PAGE_SIZE;

            if rounded >= 1024 {
                assert!(
                    span <= max_span,
                    "class {} ({} bytes) should not pin more than {} bytes per slab, got {}",
                    idx,
                    rounded,
                    max_span,
                    span
                );
            }

            if rounded > 8192 {
                let slots = span / rounded;
                assert_eq!(
                    slots, 1,
                    "class {} ({} bytes) should use one-slot slabs to bound one-live-object external fragmentation",
                    idx,
                    rounded
                );
            }
        }
    }

    #[test]
    fn over_8k_classes_use_minimum_whole_page_one_slot_spans() {
        for idx in 1..TOTAL_SIZE_CLASS {
            let rounded = get_rounded_size_by_idx(idx);
            if rounded <= 8192 {
                continue;
            }

            let pages = get_num_pages_by_idx(idx);
            let expected_pages = ceil_div_usize(rounded, crate::PAGE_SIZE);
            assert_eq!(
                pages, expected_pages,
                "class {} ({} bytes) should reserve only the minimum page span",
                idx, rounded
            );

            let (slot_count, _stride) = crate::sc::checked_size_class_geometry(rounded, pages)
                .unwrap_or_else(|| {
                    panic!(
                        "class {} ({} bytes, {} pages) has invalid geometry",
                        idx, rounded, pages
                    )
                });
            assert_eq!(
                slot_count, 1,
                "class {} ({} bytes) should expose exactly one slot per span",
                idx, rounded
            );
        }
    }

    #[test]
    fn size_class_internal_fragmentation_stays_bounded_for_supported_requests() {
        for request in 1..=MAX_SIZE {
            let (class, rounded) = get_sizeclass_tuple2(request);
            let idx = match class {
                SizeClass::Base(idx) => idx,
                SizeClass::Large(size) => {
                    panic!("unexpected large class for {}: {}", request, size)
                }
            };
            assert!(idx < TOTAL_SIZE_CLASS);
            assert!(rounded >= request);
            let slack = rounded - request;
            assert!(
                slack <= core::cmp::max(8, request / 3),
                "request {} rounded to {}; excessive internal fragmentation",
                request,
                rounded
            );
        }
    }
}
