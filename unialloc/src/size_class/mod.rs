mod mi_size_class;
mod small_size_class;
mod tc_size_class;

#[derive(Debug, PartialEq, Copy, Clone)]
pub enum SizeClass {
    Base(usize),
    Large(usize),
}

impl SizeClass {
    pub const fn inner(self) -> usize {
        match self {
            SizeClass::Base(cl) => cl,
            SizeClass::Large(cl) => cl,
        }
    }

    pub const fn index(&self) -> usize {
        match self {
            SizeClass::Base(cl) => *cl,
            SizeClass::Large(cl) => *cl,
        }
    }
}

// use crate::size_class::get_sizeclass_tuple2;
#[cfg(not(feature = "fixed_heap"))]
pub use tc_size_class::*;
// pub use mi_size_class::*;
#[cfg(feature = "fixed_heap")]
pub use small_size_class::*;

// pub use mi_size_class as size_class;

// pub trait GlobalSizeclass {
//     /// Gets the corresponding size class by requested size
//     fn get_size_class(sz: usize) -> SizeClass;

//     /// Gets the corresponding size by size class index
//     fn get_rounded_size_by_idx(idx: usize) -> usize;

//     /// Gets number of pages of by size class index
//     fn get_num_pages_by_idx(idx: usize) -> usize;

//     /// Rounds the size to allocation size
//     fn get_rounded_size(sz: usize) -> usize {
//         let idx = Self::get_size_class(sz).index();
//         Self::get_rounded_size_by_idx(idx)
//     }

//     /// Gets a tuple of size class and rounded size
//     fn get_size_class_tuple(sz: usize) -> (SizeClass, usize) {
//         let cl = Self::get_size_class(sz);
//         (cl, Self::get_rounded_size_by_idx(cl.index()))
//     }
// }

/// Rounds the size to allocation size
pub fn get_rounded_size(sz: usize) -> usize {
    let idx = get_size_class(sz).index();
    get_rounded_size_by_idx(idx)
}

/// Gets a tuple of size class and rounded size
pub fn get_size_class_tuple(sz: usize) -> (SizeClass, usize) {
    let cl = get_size_class(sz);
    (cl, get_rounded_size_by_idx(cl.index()))
}
