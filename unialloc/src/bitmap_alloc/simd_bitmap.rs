use core::arch::x86_64::*;

use crate::{print, println};

const lt_cnt: [u8; 256] = [
    0, 1, 1, 2, 1, 2, 2, 3, 1, 2, 2, 3, 2, 3, 3, 4, 1, 2, 2, 3, 2, 3, 3, 4, 2, 3, 3, 4, 3, 4, 4, 5,
    1, 2, 2, 3, 2, 3, 3, 4, 2, 3, 3, 4, 3, 4, 4, 5, 2, 3, 3, 4, 3, 4, 4, 5, 3, 4, 4, 5, 4, 5, 5, 6,
    1, 2, 2, 3, 2, 3, 3, 4, 2, 3, 3, 4, 3, 4, 4, 5, 2, 3, 3, 4, 3, 4, 4, 5, 3, 4, 4, 5, 4, 5, 5, 6,
    2, 3, 3, 4, 3, 4, 4, 5, 3, 4, 4, 5, 4, 5, 5, 6, 3, 4, 4, 5, 4, 5, 5, 6, 4, 5, 5, 6, 5, 6, 6, 7,
    1, 2, 2, 3, 2, 3, 3, 4, 2, 3, 3, 4, 3, 4, 4, 5, 2, 3, 3, 4, 3, 4, 4, 5, 3, 4, 4, 5, 4, 5, 5, 6,
    2, 3, 3, 4, 3, 4, 4, 5, 3, 4, 4, 5, 4, 5, 5, 6, 3, 4, 4, 5, 4, 5, 5, 6, 4, 5, 5, 6, 5, 6, 6, 7,
    2, 3, 3, 4, 3, 4, 4, 5, 3, 4, 4, 5, 4, 5, 5, 6, 3, 4, 4, 5, 4, 5, 5, 6, 4, 5, 5, 6, 5, 6, 6, 7,
    3, 4, 4, 5, 4, 5, 5, 6, 4, 5, 5, 6, 5, 6, 6, 7, 4, 5, 5, 6, 5, 6, 6, 7, 5, 6, 6, 7, 6, 7, 7, 8,
];

/// Computs prefix sums for 8 64bits number in parallel
fn prefixsum_m512i(x: __m512i) -> __m512i {
    unsafe {
        let i1: __m512i = _mm512_set_epi64(6, 5, 4, 3, 2, 1, 0, 7);
        let i2: __m512i = _mm512_set_epi64(5, 4, 3, 2, 1, 0, 7, 6);
        let i3: __m512i = _mm512_set_epi64(3, 2, 1, 0, 7, 6, 5, 4);

        let mut x = _mm512_add_epi64(x, _mm512_maskz_permutexvar_epi64(0b11111110, i1, x));
        x = _mm512_add_epi64(x, _mm512_maskz_permutexvar_epi64(0b11111100, i2, x));
        x = _mm512_add_epi64(x, _mm512_maskz_permutexvar_epi64(0b11110000, i3, x));

        x
    }
}

fn popcnt_m512i(v: __m512i) -> __m512i {
    unsafe {
        let m1: __m512i = _mm512_set1_epi8(0x55);
        let m2: __m512i = _mm512_set1_epi8(0x33);
        let m4: __m512i = _mm512_set1_epi8(0x0f);

        let b = _mm512_and_epi64(_mm512_srli_epi16(v, 1), m1);

        let t1 = _mm512_sub_epi8(v, b);

        let a = _mm512_and_epi64(t1, m2);
        let b = _mm512_and_epi64(_mm512_srli_epi16(t1, 2), m2);
        let t2 = _mm512_add_epi8(a, b);

        let t3 = _mm512_add_epi8(t2, _mm512_srli_epi16(t2, 4));

        let t3 = _mm512_and_epi64(t3, m4);

        _mm512_sad_epu8(t3, _mm512_setzero_si512())
    }
}

// debug print
fn print_f64x8(desc: &str, x: __m512i) {
    print!("Printing ... {}: ", desc);
    let s = core::simd::i64x8::from(x);
    for i in s.as_array() {
        print!("{}, ", i);
    }
    println!("");
}

/// Returns the position of i-th bit-set
///
/// Example:
///            x: [0, 0, 0, 1, 1, 0, 1, 0]
///        index: [7, 6, 5, 4, 3, 2, 1, 0]
/// bitset_index: [         2  1     0   ]                         
///
/// In this case,
/// `select_u64(x, 2)` = 4,
/// and `select_u64(x, 1) = 3`
///
/// ```
/// use crate::bitmap_alloc::simd_bitmap::select_u64;
/// let x = 0b00011010 as u64;
/// assert_eq!(select_u64(x, 2), 4);
/// assert_eq!(select_u64(x, 1), 3);
///
/// // another testcase
/// let x = 12u64; // [1, 1, 0, 0]
/// assert_eq!(select_u64(x, 1), 3);
/// ```
///
/// Notes:
/// Available on x86-64 and target feature bmi1 and bmi2 only
#[cfg(target_arch = "x86_64")]
fn select_u64(x: u64, i: u64) -> u64 {
    unsafe { _tzcnt_u64(_pdep_u64(1 << i, x)) }
}

/// A bitmap with 512 bits optimized with SIMD instructions
#[repr(C)]
pub struct SIMDBitmap512 {
    prefixSum: [u64; 8],
    bits: [u64; 8],
}

impl SIMDBitmap512 {
    pub fn new() -> Self {
        Self {
            prefixSum: [0u64; 8],
            bits: [0u64; 8],
        }
    }

    pub fn real_size(&self) -> u64 {
        self.prefixSum[7]
    }

    pub fn is_full(&self) -> bool {
        self.prefixSum[7] == 512
    }

    pub fn fill_zero(&mut self) {
        self.prefixSum.fill(0);
        self.bits.fill(0);
    }

    /// Gets the n-th bit (n >= && n <= 511)
    pub fn get(&self, n: usize) -> bool {
        (self.bits[(n / 64) as usize] & (1 << (n % 64))) != 0
    }

    /// Flips the n-th bit
    pub fn flip(&mut self, n: usize) {
        let w = n / 64;
        let offset = n & 63;
        self.bits[w] ^= 1u64 << offset;

        // recompute the prefix sum
        self.compute_prefix_sum();
    }

    /// Returns the index of first bit-set
    pub fn index_fs(&self) -> u64 {
        self.select(0)
    }

    /// Returns the undelying raw bitmap
    pub fn bits(&self) -> &[u64; 8] {
        &self.bits
    }

    /// Selects k-th non-zero bit
    pub fn select(&self, k: i64) -> u64 {
        // (1) popcnt each 64bit number
        // let ptr = self.bits.as_ptr() as *const i32;
        let res: u64 = unsafe {
            _mm_prefetch(self.bits.as_ptr() as *const i8, _MM_HINT_T0);
            // let mx = _mm512_loadu_si512(ptr);
            // let msums = prefixsum_m512i(popcnt_m512i(mx));

            let msums = _mm512_loadu_si512(self.prefixSum.as_ptr() as *const i32);

            // print_f64x8("msums", msums);

            let mk = _mm512_set_epi64(k, k, k, k, k, k, k, k);
            // print_f64x8("mk   ", mk);
            let mask = _mm512_cmple_epi64_mask(msums, mk);

            // println!("mask: {}", mask);

            let i = lt_cnt[mask as usize] as usize;

            let sums = [0u64; 9];
            let p = sums.as_ptr();

            // slot[0] performs as a sentinel
            _mm512_storeu_si512(p.offset(1) as *mut i32, msums);

            // to make it branchless, we don't have any check here
            // therefore please use it carefully
            let x = self.bits[i] as u64;
            let idx = k as u64 - sums[i];

            i as u64 * 64 + select_u64(x, idx)
        };

        res
    }

    fn compute_prefix_sum(&mut self) {
        let ptr = self.bits.as_ptr() as *const i32;
        unsafe {
            let mx = _mm512_loadu_si512(ptr);
            let msums = prefixsum_m512i(popcnt_m512i(mx));

            _mm512_storeu_si512(self.prefixSum.as_ptr() as *mut i32, msums);
        }
    }
}

const BITMAP_FULL: u64 = !0u64;

// A bitmap supporting contiguous zero search
struct ContBitmap512 {
    prefixSum: [u64; 8],
    bits: [u64; 8],
    // popcnts: [u8; 8],
    last_pos: u16,
    first_zero: u16,
}

impl ContBitmap512 {
    /// Gets the underlying raw bits
    pub fn bits(&self) -> &[u64; 8] {
        &self.bits
    }

    /// Finds a sequence having n zero bits
    pub fn search_zero_seq(&self, len: usize) {
        let mask = (1u64 << len) - 1;
        let map = self.bits[0];

        let trailing_ones = !map.trailing_zeros();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanity() {
        let mut b = SIMDBitmap512::new();
        let i = 64usize;

        assert_eq!(b.get(i), false);
        b.flip(i);
        assert_eq!(b.get(i), true);
        b.flip(511);

        assert_eq!(b.index_fs(), i as u64);
        assert_eq!(b.select(1), 511);
    }
}
