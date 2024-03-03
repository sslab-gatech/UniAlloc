use super::SizeClass;
use crate::*;
use core::alloc::Layout;
use core::intrinsics::{likely, unlikely};

// #[inline]
// fn get_aligned_layout(layout: &Layout) -> Option<Layout> {
//     let align = layout.align();
//     let new_size = get_size_class(layout.size());
//     Layout::from_size_align(new_size, align).ok()
// }

pub(crate) const NUM_SIZE_CLASSES: usize = 63;
const MAX_LEN: usize = 63;
pub const MAX_SIZE: usize = 28032;

/// Returns index and size_class
/// The time of binary_search is `O(log(MAX_LEN))`
/// The time of calculating size is `O(1)`
#[inline]
pub fn get_index_and_size(req_size: usize) -> (SizeClass, usize) {
    get_sizeclass_tuple2(req_size)
}

#[inline]
pub fn get_size_from_idx(idx: usize) -> usize {
    SIZE_ARRAY[idx] as usize
}

#[inline]
fn binary_search_size(arr: &[usize; MAX_LEN + 1], target: usize) -> Option<usize> {
    if target > MAX_SIZE {
        return None;
    }

    let mut left: i32 = 0;
    let mut right: i32 = MAX_LEN as i32 - 1;
    while left <= right {
        let mid = left + (right - left) / 2;

        if arr[mid as usize] < target {
            left = mid + 1;
        } else if arr[mid as usize] >= target {
            right = mid - 1;
        }
    }

    // println!("target: {}, mid: {}, left: {}, right: {}, arr[mid]: {}", target, mid, left, right, arr[mid as usize]);

    if arr[left as usize] == target {
        Some(left as usize)
    } else {
        Some(left as usize - 1)
    }
}

/// # Deprecated
///
/// Get the size class based on the request size
///
/// # Safety:
///
/// we need check the req_size is not overflow before calling this
///
/// e.g., using checked_add() to caculate the request size
fn get_size_class_index(req_size: usize) -> SizeClass {
    match req_size {
        0..=8 => SizeClass::Base(0),
        9..=16 => SizeClass::Base(1),
        17..=24 => SizeClass::Base(2),
        25..=32 => SizeClass::Base(3),
        33..=40 => SizeClass::Base(4),
        41..=48 => SizeClass::Base(5),
        49..=56 => SizeClass::Base(6),
        57..=64 => SizeClass::Base(7),
        65..=72 => SizeClass::Base(8),
        73..=80 => SizeClass::Base(9),
        81..=88 => SizeClass::Base(10),
        89..=96 => SizeClass::Base(11),
        97..=104 => SizeClass::Base(12),
        105..=112 => SizeClass::Base(13),
        113..=120 => SizeClass::Base(14),
        121..=128 => SizeClass::Base(15),
        129..=144 => SizeClass::Base(16),
        145..=160 => SizeClass::Base(17),
        161..=176 => SizeClass::Base(18),
        177..=192 => SizeClass::Base(19),
        193..=208 => SizeClass::Base(20),
        209..=224 => SizeClass::Base(21),
        225..=240 => SizeClass::Base(22),
        241..=256 => SizeClass::Base(23),
        257..=280 => SizeClass::Base(24),
        281..=304 => SizeClass::Base(25),
        305..=352 => SizeClass::Base(26),
        353..=384 => SizeClass::Base(27),
        385..=424 => SizeClass::Base(28),
        425..=480 => SizeClass::Base(29),
        481..=512 => SizeClass::Base(30),
        513..=576 => SizeClass::Base(31),
        577..=640 => SizeClass::Base(32),
        641..=704 => SizeClass::Base(33),
        705..=832 => SizeClass::Base(34),
        833..=896 => SizeClass::Base(35),
        897..=1024 => SizeClass::Base(36),
        1025..=1152 => SizeClass::Base(37),
        1153..=1280 => SizeClass::Base(38),
        1281..=1408 => SizeClass::Base(39),
        1409..=1536 => SizeClass::Base(40),
        1537..=1792 => SizeClass::Base(41),
        1793..=2048 => SizeClass::Base(42),
        2049..=2176 => SizeClass::Base(43),
        2177..=2304 => SizeClass::Base(44),
        2305..=2432 => SizeClass::Base(45),
        2433..=2944 => SizeClass::Base(46),
        2945..=3200 => SizeClass::Base(47),
        3201..=3584 => SizeClass::Base(48),
        3585..=4096 => SizeClass::Base(49),
        4097..=4608 => SizeClass::Base(50),
        4609..=5376 => SizeClass::Base(51),
        5377..=6528 => SizeClass::Base(52),
        6529..=8192 => SizeClass::Base(53),
        8193..=9344 => SizeClass::Base(54),
        9345..=10880 => SizeClass::Base(55),
        10881..=13056 => SizeClass::Base(56),
        13057..=13952 => SizeClass::Base(57),
        13953..=16384 => SizeClass::Base(58),
        16385..=19072 => SizeClass::Base(59),
        19073..=21760 => SizeClass::Base(60),
        21761..=24576 => SizeClass::Base(61),
        24577..=28032 => SizeClass::Base(62),
        _ => SizeClass::Large(req_size),
    }
}

pub fn get_size_class(req: usize) -> SizeClass {
    get_sizeclass_tuple2(req).0
}
const MAX_ALLOC_SIZE: usize = 28032;
const IDX_ARRAY: [u8; ((MAX_ALLOC_SIZE + 127 + (120 << 7)) >> 7) + 1] = [
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

const SIZE_ARRAY: [u16; 64] = [
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
        let new_size = SIZE_ARRAY[new_size_idx] as usize;
        (SizeClass::Base(new_size_idx), new_size)
    } else if req_size <= MAX_SIZE {
        let idx = (req_size + 127 + (120 << 7)) >> 7;
        let new_size_idx = IDX_ARRAY[idx] as usize;
        let new_size = SIZE_ARRAY[new_size_idx] as usize;
        (SizeClass::Base(new_size_idx), new_size)
    } else {
        (SizeClass::Large(req_size), req_size)
    }
}

//Below are proposed API for size class

const SIZE_CLASSES: [usize; TOTAL_SIZE_CLASS] = [
    0, 8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144, 160, 176, 192, 208,
    224, 240, 256, 280, 304, 352, 384, 424, 480, 512, 576, 640, 704, 832, 896, 1024, 1152, 1280,
    1408, 1536, 1792, 2048, 2176, 2304, 2432, 2944, 3200, 3584, 4096, 4608, 5376, 6528, 8192, 9344,
    10880, 13056, 13952, 16384, 19072, 21760, 24576, 28032,
];

const SIZE_CLASS_PAGES: [usize; TOTAL_SIZE_CLASS] = [
    0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 4,
    4, 4, 4, 4, 4, 8, 8, 8, 8, 8, 8, 16, 16, 16, 16, 16, 16, 16, 32, 32, 32, 32, 64, 64, 64, 64,
    64, 64, 64, 64, 64, 64,
];

pub const TOTAL_SIZE_CLASS: usize = 64;

pub const BACKEND_MAX_PAGE: usize = 128;

pub fn get_rounded_size_by_idx(idx: usize) -> usize {
    SIZE_CLASSES[idx]
}

pub fn get_size_class_by_idx(idx: usize) -> usize {
    SIZE_CLASSES[idx]
}

pub fn get_num_pages_by_idx(idx: usize) -> usize {
    SIZE_CLASS_PAGES[idx]
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::size_class::get_rounded_size;

    // #[test]
    // fn it_works() {
    //     let l0 = Layout::from_size_align(4, 4).expect("cannot create");
    //     let l1 = Layout::from_size_align(30, 4).expect("cannot create");
    //     let aligned_l0 = get_aligned_layout(&l0).expect("cannot create");
    //     let aligned_l1 = get_aligned_layout(&l1).expect("cannot create");
    //     assert_eq!(aligned_l0.size(), 8);
    //     assert_eq!(aligned_l1.size(), 32);
    // }

    // #[test]
    // fn it_tuple_works() {
    //     for i in 137..30000 {
    //         let a = get_sizeclass_tuple(i as usize);
    //         let b = get_sizeclass_tuple2(i as usize);
    //         assert_eq!(a.1, b.1)
    //     }
    // }

    // #[test]
    // fn get_index_size_sanity_check() {
    //     assert_eq!((SizeClass::Base(0), 8usize), get_index_and_size(0));
    //     assert_eq!((SizeClass::Base(0), 8usize), get_index_and_size(1));
    //     assert_eq!((SizeClass::Base(62), 28032usize), get_index_and_size(28032));
    //     assert_eq!(
    //         (SizeClass::Large(28033), 28033usize),
    //         get_index_and_size(28033)
    //     );
    // }
    //
    // #[test]
    // fn get_index_size_full_test() {
    //     for i in 0..MAX_SIZE + 0x1000 {
    //         let sclass = get_size_class_index(i);
    //         let sz = get_rounded_size(i);
    //         assert_eq!((sclass, sz), get_index_and_size(i), "size: {}", i);
    //     }
    // }
}
