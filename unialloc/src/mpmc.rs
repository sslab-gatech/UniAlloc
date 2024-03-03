use core::{cell::UnsafeCell, mem::MaybeUninit};
type AtomicTargetSize = core::sync::atomic::AtomicUsize;
type Ordering = core::sync::atomic::Ordering;
type IntSize = usize;

#[allow(dead_code)]
/// Const assert hack
pub struct Assert<const L: usize, const R: usize>;

#[allow(dead_code)]
impl<const L: usize, const R: usize> Assert<L, R> {
    /// Const assert hack
    pub const GREATER_EQ: usize = L - R;

    /// Const assert hack
    pub const LESS_EQ: usize = R - L;

    /// Const assert hack
    pub const NOT_EQ: isize = 0 / (R as isize - L as isize);

    /// Const assert hack
    pub const EQ: usize = (R - L) + (L - R);

    /// Const assert hack
    pub const GREATER: usize = L - R - 1;

    /// Const assert hack
    pub const LESS: usize = R - L - 1;

    /// Const assert hack
    pub const POWER_OF_TWO: usize = 0 - (L & (L - 1));
}

/// MPMC queue with a capability for 2 elements.
pub type Q2<T> = MpMcQueue<T, 2>;

/// MPMC queue with a capability for 4 elements.
pub type Q4<T> = MpMcQueue<T, 4>;

/// MPMC queue with a capability for 8 elements.
pub type Q8<T> = MpMcQueue<T, 8>;

/// MPMC queue with a capability for 16 elements.
pub type Q16<T> = MpMcQueue<T, 16>;

/// MPMC queue with a capability for 32 elements.
pub type Q32<T> = MpMcQueue<T, 32>;

/// MPMC queue with a capability for 64 elements.
pub type Q64<T> = MpMcQueue<T, 64>;

/// MPMC queue with a capability for 128 elements.
pub type Q128<T> = MpMcQueue<T, 128>;

/// MPMC queue with a capability for 256 elements.
pub type Q256<T> = MpMcQueue<T, 256>;

/// MPMC queue with a capability for 512 elements.
pub type Q512<T> = MpMcQueue<T, 512>;

/// MPMC queue with a capacity for N elements
/// N must be a power of 2
/// The max value of N is u8::MAX - 1 if `mpmc_large` feature is not enabled.
pub struct MpMcQueue<T, const N: usize> {
    buffer: UnsafeCell<[Cell<T>; N]>,
    dequeue_pos: AtomicTargetSize,
    enqueue_pos: AtomicTargetSize,
}

impl<T, const N: usize> MpMcQueue<T, N> {
    const MASK: IntSize = (N - 1) as IntSize;
    const EMPTY_CELL: Cell<T> = Cell::new(0);

    const ASSERT: [(); 1] = [()];

    /// Creates an empty queue
    pub const fn new() -> Self {
        // Const assert
        Assert::<N, 1>::GREATER;
        Assert::<N, 0>::GREATER;
        Assert::<N, 0>::POWER_OF_TWO;

        // Const assert on size.
        Self::ASSERT[!(N < (IntSize::MAX as usize)) as usize];

        let mut cell_count = 0;

        let mut result_cells: [Cell<T>; N] = [Self::EMPTY_CELL; N];
        while cell_count != N {
            result_cells[cell_count] = Cell::new(cell_count);
            cell_count += 1;
        }

        Self {
            buffer: UnsafeCell::new(result_cells),
            dequeue_pos: AtomicTargetSize::new(0),
            enqueue_pos: AtomicTargetSize::new(0),
        }
    }

    /// Returns the item in the front of the queue, or `None` if the queue is empty
    pub fn dequeue(&self) -> Option<T> {
        unsafe { dequeue(self.buffer.get() as *mut _, &self.dequeue_pos, Self::MASK) }
    }

    /// Adds an `item` to the end of the queue
    ///
    /// Returns back the `item` if the queue is full
    pub fn enqueue(&self, item: T) -> Result<(), T> {
        unsafe {
            enqueue(
                self.buffer.get() as *mut _,
                &self.enqueue_pos,
                Self::MASK,
                item,
            )
        }
    }
}

impl<T, const N: usize> Default for MpMcQueue<T, N> {
    fn default() -> Self {
        Self::new()
    }
}

unsafe impl<T, const N: usize> Sync for MpMcQueue<T, N> where T: Send {}

struct Cell<T> {
    data: MaybeUninit<T>,
    sequence: AtomicTargetSize,
}

impl<T> Cell<T> {
    const fn new(seq: usize) -> Self {
        Self {
            data: MaybeUninit::uninit(),
            sequence: AtomicTargetSize::new(seq as IntSize),
        }
    }
}

unsafe fn dequeue<T>(
    buffer: *mut Cell<T>,
    dequeue_pos: &AtomicTargetSize,
    mask: IntSize,
) -> Option<T> {
    let mut pos = dequeue_pos.load(Ordering::Relaxed);

    let mut cell;
    loop {
        cell = buffer.add(usize::from(pos & mask));
        let seq = (*cell).sequence.load(Ordering::Acquire);
        let dif = (seq as i8).wrapping_sub((pos.wrapping_add(1)) as i8);

        if dif == 0 {
            if dequeue_pos
                .compare_exchange_weak(
                    pos,
                    pos.wrapping_add(1),
                    Ordering::Relaxed,
                    Ordering::Relaxed,
                )
                .is_ok()
            {
                break;
            }
        } else if dif < 0 {
            return None;
        } else {
            pos = dequeue_pos.load(Ordering::Relaxed);
        }
    }

    let data = (*cell).data.as_ptr().read();
    (*cell)
        .sequence
        .store(pos.wrapping_add(mask).wrapping_add(1), Ordering::Release);
    Some(data)
}

unsafe fn enqueue<T>(
    buffer: *mut Cell<T>,
    enqueue_pos: &AtomicTargetSize,
    mask: IntSize,
    item: T,
) -> Result<(), T> {
    let mut pos = enqueue_pos.load(Ordering::Relaxed);

    let mut cell;
    loop {
        cell = buffer.add(usize::from(pos & mask));
        let seq = (*cell).sequence.load(Ordering::Acquire);
        let dif = (seq as i8).wrapping_sub(pos as i8);

        if dif == 0 {
            if enqueue_pos
                .compare_exchange_weak(
                    pos,
                    pos.wrapping_add(1),
                    Ordering::Relaxed,
                    Ordering::Relaxed,
                )
                .is_ok()
            {
                break;
            }
        } else if dif < 0 {
            return Err(item);
        } else {
            pos = enqueue_pos.load(Ordering::Relaxed);
        }
    }

    (*cell).data.as_mut_ptr().write(item);
    (*cell)
        .sequence
        .store(pos.wrapping_add(1), Ordering::Release);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::Q2;

    #[test]
    fn sanity() {
        let q = Q2::<usize>::new();
        q.enqueue(0).unwrap();
        q.enqueue(1).unwrap();
        assert!(q.enqueue(2).is_err());

        assert_eq!(q.dequeue(), Some(0));
        assert_eq!(q.dequeue(), Some(1));
        assert_eq!(q.dequeue(), None);
    }

    #[test]
    fn drain_at_pos255() {
        let q = Q2::new();
        for _ in 0..255 {
            assert!(q.enqueue(0).is_ok());
            assert_eq!(q.dequeue(), Some(0));
        }
        // this should not block forever
        assert_eq!(q.dequeue(), None);
    }

    #[test]
    fn full_at_wrapped_pos0() {
        let q = Q2::new();
        for _ in 0..254 {
            assert!(q.enqueue(0).is_ok());
            assert_eq!(q.dequeue(), Some(0));
        }
        assert!(q.enqueue(0).is_ok());
        assert!(q.enqueue(0).is_ok());
        // this should not block forever
        assert!(q.enqueue(0).is_err());
    }
}
