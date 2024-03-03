#[derive(Debug)]
pub struct AllocError(i32);

impl AllocError {
    /// Out of memory
    pub const ENOMEM: Self = AllocError(-1i32);

    /// Double free
    pub const EDBFRE: Self = AllocError(-2i32);

    /// use after free
    pub const EUAF: Self = AllocError(-3i32);

    /// Bad layout
    pub const ELAYOUT: Self = AllocError(-4i32);

    /// Bad size
    pub const ESIZE: Self = AllocError(-5i32);

    /// Out of bounds
    pub const EOOB: Self = AllocError(-6i32);

    /// cpu migration (wrong cpu)
    pub const ECPU: Self = AllocError(-7i32);

    /// Fatal errors
    pub const EFATAL: Self = AllocError(-8i32);

    pub fn to_raw_errno(&self) -> i32 {
        self.0
    }
}

pub type Result<T> = core::result::Result<T, AllocError>;
