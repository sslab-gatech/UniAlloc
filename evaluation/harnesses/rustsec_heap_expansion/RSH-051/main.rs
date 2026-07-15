//! RUSTSEC-2019-0021 / linea 0.9.4.
//!
//! Upstream issue 1's published `Matrix::scale` reproducer. The matrix is
//! assembled through the public constructor so the witness remains valid when
//! the experiment runner places it in a module.
//! The repository applies a hash-pinned current-toolchain compatibility patch
//! to both arms and the exact upstream PR-2 source fix to the patched arm.

use generic_array::GenericArray;
use linea::Matrix;
use std::ops::Mul;
use typenum::consts::U1;

#[derive(Debug)]
struct Item(Vec<i32>);

#[derive(Copy, Clone)]
struct Scale;

impl Mul<Scale> for Item {
    type Output = Scale;

    fn mul(self, _: Scale) -> Self::Output {
        panic!("published Mul panic")
    }
}

impl Drop for Item {
    fn drop(&mut self) {
        eprintln!("dropped {:?}", self.0);
    }
}

fn main() {
    let column: GenericArray<Item, U1> =
        GenericArray::from_exact_iter(std::iter::once(Item(vec![1, 2, 3]))).unwrap();
    let columns: GenericArray<GenericArray<Item, U1>, U1> =
        GenericArray::from_exact_iter(std::iter::once(column)).unwrap();
    let mat: Matrix<Item, U1, U1> = Matrix::from_col_major_array(columns);
    mat.scale(Scale);
}
