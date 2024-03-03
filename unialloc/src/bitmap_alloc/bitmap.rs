use core::arch::x86_64::*;
use core::ops::{Add, AddAssign, Index, IndexMut, Mul};

use libc::DEBUGFS_MAGIC;

// 2000K ~ 2048K

// A raw bitmap with 512 bits
type RawBitmap512 = RawBitmap<64>;

/// A zero-indexed bitmap
///
/// `N` represnts the bytes of current bitmap
/// e.g., The N for a 512 bits bitmap should be 64
#[repr(C)]
struct RawBitmap<const N: usize> {
    bits: [u8; N],
}

impl<const N: usize> RawBitmap<N> {
    pub fn new() -> Self {
        Self { bits: [0u8; N] }
    }

    // get nth bit
    pub fn get_bit(&self, n: usize) -> bool {
        (self.bits[n / 8] & (1 << (n % 8))) != 0
    }

    // get nth bit
    pub fn set_bit(&mut self, n: usize) {
        self.bits[n / 8] |= 1 << (n % 8);
    }

    // unset nth bit
    pub fn unset_bit(&mut self, n: usize) {
        self.bits[n / 8] &= !(1 << (n % 8));
    }
}

#[repr(C)]
pub struct LazySegTree<const N: usize>
where
    [u32; 2 * N]: Sized,
{
    /// Lazy tag
    tag: [u32; 2 * N],

    /// Moniod sum:
    sum: [u32; 2 * N],

    /// The number of elements in the subtree
    ///
    /// Used when len is not order of 2
    /// When adding an integer to a subtree, we need to know the number of
    /// elements in the subtree to compute its influence on the monoid sum.
    ///
    /// Initially, [n, 2n) have 1 element, and their parents are set accordingly
    num: [u32; 2 * N],
}

impl<const N: usize> LazySegTree<N>
where
    [u32; 2 * N]: Sized,
{
    const ln: u32 = 31 - (N - 1).leading_zeros() + 1;

    pub fn new() {
        let tag = [0u8; 2 * N];
    }

    /// Applies a lazy tag to node i
    fn apply(&mut self, i: usize, v: u32) {
        // because the current node represents an interval, when
        // we wanna add a value to a range, the monoid sum can be modified
        // with [number of elements] * [value to be added]
        self.sum[i] += self.num[i] * v;

        // set the lazy tag of current node
        self.tag[i] += v;
    }

    /// Pushes down the lazy tag
    fn untag(&mut self, i: usize) {
        let mut h = Self::ln;

        while h != 0 {
            let j = i >> h;
            if j != 0 && self.tag[j] != 0 {
                self.apply(2 * j, self.tag[j]);
                self.apply(2 * j + 1, self.tag[j]);
                self.tag[j] = 0;
            }
            h -= 1;
        }
    }

    // Aggregates 2*i and 2*i+1 to i
    fn mconcat(&mut self, l: usize) {
        // 2*i <+> s*i+1 -> i
        self.sum[l >> 1] = self.sum[l] + self.sum[l ^ 1];
    }

    /// Adds number v to range [l, r)
    pub fn add(&mut self, l: usize, r: usize, v: u32) {
        // Converts half-open interval [l, r)
        // to (l, r)
        let mut l = l + N - 1;
        let mut r = r + N;

        // Pushes down lazy tags
        self.untag(l + 1);
        self.untag(r - 1);

        let mut lf = false;
        let mut rf = false;

        while (l ^ r ^ 1) != 0 {
            // if l is even
            if (!l & 1) != 0 {
                // apply lazy tag to node l+1
                self.apply(l ^ 1, v);
                lf = true;
            }

            // if r is odd
            if (r & 1) != 0 {
                // apply lazy tag to node r-1
                self.apply(r ^ 1, v);
                rf = true;
            }

            if lf && l > 1 {
                self.mconcat(l);
            }
            if rf {
                self.mconcat(r);
            }

            // traversal parents
            l >>= 1;
            r >>= 1;
        }

        while l > 1 {
            self.mconcat(l);
            l >>= 1;
        }
    }

    // Gets sum of range [l, r)
    pub fn get_sum(&mut self, l: usize, r: usize) -> u32 {
        // Convert half-open interval to full-open interval
        let mut l = l + N - 1;
        let mut r = r + N;

        self.untag(l + 1);
        self.untag(r - 1);

        let mut ls = 0;
        let mut rs = 0;

        while (l ^ r ^ 1) != 0 {
            // for left side
            // if [index is even] -> add [index+1]
            if (!l & 1) != 0 {
                ls += self.sum[l ^ 1];
            }

            // for right side
            // if [index is odd] -> add [index-1]
            if (r & 1) != 0 {
                rs = self.sum[r ^ 1] + rs;
            }

            // traversal parents
            l >>= 1;
            r >>= 1;
        }

        ls + rs
    }
}

#[repr(C)]
pub struct CommutativeTree<const N: usize>
where
    [u32; 2 * N]: Sized,
{
    tag: [u32; 2 * N],
    sum: [u32; 2 * N],
}

impl<const N: usize> CommutativeTree<N>
where
    [u32; 2 * N]: Sized,
{
    fn new() -> Self {
        Self {
            tag: [0u32; 2 * N],
            sum: [0u32; 2 * N],
        }
    }

    fn add(&mut self, l: usize, r: usize, v: u32) {
        // Converts half-open interval to full-open interval
        let mut l = l + N - 1;
        let mut r = r + N;

        let mut lc = 0;
        let mut rc = 0;
        let mut k = 1;

        while l ^ r ^ 1 != 0 {
            if (!l & 1) != 0 {
                self.sum[l ^ 1] += v * k;
                self.tag[l ^ 1] += v;
                lc += k;
            }

            if (r & 1) != 0 {
                self.sum[r ^ 1] += v * k;
                self.tag[r ^ 1] += v;
                rc += k;
            }

            self.sum[l >> 1] += v * lc;
            self.sum[r >> 1] += v * rc;

            l >>= 1;
            r >>= 1;
            k <<= 1;
        }

        while l > 1 {
            self.sum[l] += v * lc;
            l >>= 1;

            self.sum[r] += v * rc;
            r >>= 1;
        }
    }

    fn get_sum(&mut self, l: usize, r: usize) -> u32 {
        // Converts half-open to full-open
        let mut l = l + N - 1;
        let mut r = r + N;

        let mut ans = 0;
        let mut lc = 0;
        let mut rc = 0;

        let mut k = 1;

        while (l ^ r ^ 1) != 0 {
            if (!l & 1) != 0 {
                ans += self.sum[l ^ 1];
                lc += k;
            }

            if (r & 1) != 0 {
                ans += self.sum[r ^ 1];
                rc += k;
            }

            ans += self.tag[l >> 1] * lc + self.tag[r >> 1] * rc;

            l >>= 1;
            r >>= 1;
            k <<= 1;
        }

        while l > 1 {
            ans += self.tag[l] * lc + self.tag[r] * rc;
            l >>= 1;
            r >>= 1;
        }

        ans
    }
}

#[repr(C)]
struct TreeNode<T>(T);

#[repr(C)]
struct CompressedSum<const N: usize>
where
    [u8; N * 8]: Sized,
{
    sum: [u8; N * 8],
    bmp: RawBitmap<N>,
}

impl<const N: usize> CompressedSum<N>
where
    [u8; N * 8]: Sized,
{
    pub fn new() -> Self {
        Self {
            sum: [0; N * 8],
            bmp: RawBitmap::<N>::new(),
        }
    }

    /// Gets idx-th bit (0-indexed)
    pub fn get(&self, idx: usize) -> u8 {
        if idx < N * 8 {
            self.sum[idx]
        } else {
            self.bmp.get_bit(idx - N * 8) as u8
        }
    }

    /// Sets idx-th bit (0-indexed)
    pub fn set(&mut self, idx: usize, val: u8) {
        if idx < N * 8 {
            self.sum[idx] = val;
        } else {
            if val == 1 {
                self.bmp.set_bit(idx - N * 8);
            } else if val == 0 {
                self.bmp.unset_bit(idx - N * 8);
            }
            unreachable!();
        }
    }

    pub fn add(&mut self, idx: usize, val: u8) {
        if idx < N * 8 {
            self.sum[idx] += val;
        } else {
            if val == 1 {
                let new_idx = idx - N * 8;
                debug_assert_eq!(self.get(idx), 0);
                self.bmp.set_bit(new_idx);
            }
            unreachable!();
        }
    }
}

/// A commutative bitmap using bitmap for storing leaf nodes
///
/// Different from [`CommutativeTree`], [`CommutativeBitmap`]
/// can only have 0 or 1
///
/// N: bytes
/// number of elements: N * 8
///
/// Currently it only supports `add` operation, which is commutative
#[repr(C)]
pub struct CommutativeBitmap<const N: usize>
where
    [u8; N * 16]: Sized,
    [u8; N * 8]: Sized,
{
    tag: [u8; N * 16],
    sum: CompressedSum<N>,
}

impl<const N: usize> CommutativeBitmap<N>
where
    [u8; N * 16]: Sized,
    [u8; N * 8]: Sized,
{
    pub fn new() -> Self {
        Self {
            tag: [0u8; N * 16],
            sum: CompressedSum::<N>::new(),
        }
    }

    fn add(&mut self, l: usize, r: usize, v: u8) {
        assert!(v <= 1);

        let mut l = l + N * 8 - 1;
        let mut r = r + N * 8;

        let mut lc = 0;
        let mut rc = 0;
        let mut k = 1;

        while l ^ r ^ 1 != 0 {
            if (!l & 1) != 0 {
                self.sum.add(l ^ 1, v * k);
                self.tag[l ^ 1] += v;
                lc += k;
            }

            if (r & 1) != 0 {
                self.sum.add(r ^ 1, v * k);
                self.tag[r ^ 1] += v;
                rc += k;
            }

            self.sum.add(l >> 1, v * lc);
            self.sum.add(r >> 1, v * rc);

            l >>= 1;
            r >>= 1;
            k <<= 1;
        }
    }

    fn get_sum(&mut self, l: usize, r: usize) -> u8 {
        // Converts half-open to full-open
        let mut l = l + N - 1;
        let mut r = r + N;

        let mut ans = 0;
        let mut lc = 0;
        let mut rc = 0;

        let mut k = 1;

        while (l ^ r ^ 1) != 0 {
            if (!l & 1) != 0 {
                ans += self.sum.get(l ^ 1);
                lc += k;
            }

            if (r & 1) != 0 {
                ans += self.sum.get(r ^ 1);
                rc += k;
            }

            ans += self.tag[l >> 1] * lc + self.tag[r >> 1] * rc;

            l >>= 1;
            r >>= 1;
            k <<= 1;
        }

        while l > 1 {
            ans += self.tag[l] * lc + self.tag[r] * rc;
            l >>= 1;
            r >>= 1;
        }

        ans
    }
}

#[cfg(test)]
mod tests {
    use crate::print;

    use super::*;
    use core::mem::transmute;

    #[test]
    fn sanity() {
        let mut bmp = RawBitmap::<64>::new();
        assert_eq!(bmp.get_bit(12), false);
        bmp.set_bit(12);
        bmp.set_bit(3);
        assert_eq!(bmp.get_bit(3), true);
        assert_eq!(bmp.get_bit(12), true);

        let v = [0b00001000u8, 0b11110011];
        let bmp = unsafe { transmute::<[u8; 2], RawBitmap<2>>(v) };

        assert_eq!(bmp.get_bit(3), true);
        assert_eq!(bmp.get_bit(8), true);
        assert_eq!(bmp.get_bit(9), true);
    }

    #[test]
    fn compress_sum_test() {}
}
