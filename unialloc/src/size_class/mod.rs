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

#[cfg(feature = "fixed_heap")]
pub use small_size_class::*;
#[cfg(not(feature = "fixed_heap"))]
pub use tc_size_class::*;

/// Rounds the size to allocation size
pub fn get_rounded_size(sz: usize) -> usize {
    match get_size_class(sz) {
        SizeClass::Base(idx) => get_rounded_size_by_idx(idx),
        SizeClass::Large(size) => size,
    }
}

/// Gets a tuple of size class and rounded size
pub fn get_size_class_tuple(sz: usize) -> (SizeClass, usize) {
    let cl = get_size_class(sz);
    let rounded = match cl {
        SizeClass::Base(idx) => get_rounded_size_by_idx(idx),
        SizeClass::Large(size) => size,
    };
    (cl, rounded)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounded_size_helpers_return_large_requests_without_table_indexing() {
        let size = usize::MAX;

        assert_eq!(get_size_class(size), SizeClass::Large(size));
        assert_eq!(get_rounded_size(size), size);
        assert_eq!(get_size_class_tuple(size), (SizeClass::Large(size), size));
    }
}
