//! Small per-thread handoff queue for semantic allocation experiments.
//!
//! The queue is intentionally minimal: it stores the next pointer and the
//! allocation size in the first two words of caller-provided storage. This is
//! useful for compiler/runtime experiments that need to pass a recently freed or
//! preclassified object to the allocator without allocating additional metadata.

use core::mem::{align_of, size_of};
use core::ptr::null_mut;

#[thread_local]
pub static mut PER_THREAD_QUEUE: *mut u8 = null_mut();
#[thread_local]
static mut PER_THREAD_QUEUE_OBJECTS: usize = 0;
#[thread_local]
static mut PER_THREAD_QUEUE_BYTES: usize = 0;

const WORDS_IN_NODE: usize = 2;

/// Minimum object size that can be linked through the per-thread queue.
pub const MIN_QUEUE_OBJECT_SIZE: usize = WORDS_IN_NODE * size_of::<usize>();
/// Maximum number of objects retained by the per-thread semantic handoff queue.
///
/// This queue is intended as a tiny compiler/runtime handoff buffer, not as a
/// second allocator cache.  A hard object cap keeps compiler experiments from
/// pinning an unbounded LIFO chain when a consumer stops draining the queue.
pub const MAX_PER_THREAD_QUEUE_OBJECTS: usize = 32;
/// Maximum bytes retained by one thread's semantic handoff queue.
///
/// Match the hot semantic cache/thread-cache target used elsewhere in
/// UniAlloc.  Larger bursts should fall back to the ordinary allocator path
/// where slabs/runs can be coalesced instead of sitting behind a TLS pointer.
pub const MAX_PER_THREAD_QUEUE_BYTES: usize = 64 * 1024;

/// Diagnostic snapshot for the per-thread semantic handoff queue.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PerThreadQueueSnapshot {
    pub objects: usize,
    pub bytes: usize,
    pub max_objects: usize,
    pub max_bytes: usize,
}

#[inline]
fn queue_storage_eligible(ptr: *mut u8, size: usize) -> bool {
    !ptr.is_null() && size >= MIN_QUEUE_OBJECT_SIZE && (ptr as usize) % align_of::<usize>() == 0
}

/// Try to push a chunk into the per-thread queue.
///
/// Returns `false` without mutating the queue when the caller-provided storage
/// is too small/misaligned or when retaining the object would exceed the
/// per-thread object/byte budget.  On `false`, ownership of `ptr` remains with
/// the caller; the queue never frees rejected storage on the caller's behalf.
///
/// # Safety
///
/// When `ptr` is non-null, suitably aligned, and `size` is large enough, it
/// must identify at least `size` bytes of live, writable storage that the
/// caller exclusively owns. The first two machine words may be overwritten by
/// the queue and the storage must remain live and exclusively reserved until
/// it is returned by [`pop_per_thread_queue`] or the queue is cleared. Null,
/// undersized, and misaligned pointers are rejected before being dereferenced.
pub unsafe fn try_push_per_thread_queue(ptr: *mut u8, size: usize) -> bool {
    if !queue_storage_eligible(ptr, size) {
        return false;
    }

    unsafe {
        let new_bytes = match PER_THREAD_QUEUE_BYTES.checked_add(size) {
            Some(bytes) => bytes,
            None => return false,
        };
        if PER_THREAD_QUEUE_OBJECTS >= MAX_PER_THREAD_QUEUE_OBJECTS
            || new_bytes > MAX_PER_THREAD_QUEUE_BYTES
        {
            return false;
        }

        let node = ptr as *mut usize;
        node.write(PER_THREAD_QUEUE as usize);
        node.add(1).write(size);
        PER_THREAD_QUEUE = ptr;
        PER_THREAD_QUEUE_OBJECTS += 1;
        PER_THREAD_QUEUE_BYTES = new_bytes;
    }
    true
}

/// Push a chunk into the per-thread queue.
///
/// Chunks smaller than two machine words are ignored because the queue needs one
/// word for the next pointer and one word for the original allocation size.
///
/// This legacy convenience wrapper intentionally drops the success bit.  New
/// callers that need to release or recycle rejected chunks should use
/// [`try_push_per_thread_queue`] instead.
///
/// # Safety
///
/// The caller must satisfy the storage, exclusivity, and lifetime contract of
/// [`try_push_per_thread_queue`].
pub unsafe fn push_per_thread_queue(ptr: *mut u8, size: usize) {
    let _ = unsafe { try_push_per_thread_queue(ptr, size) };
}

/// Pop the most recently pushed chunk from the per-thread queue.
///
/// # Safety
///
/// Every queued node must still satisfy the lifetime and exclusive-storage
/// contract established by [`try_push_per_thread_queue`]. The caller resumes
/// ownership of the returned storage and is responsible for its disposition.
pub unsafe fn pop_per_thread_queue() -> Option<(*mut u8, usize)> {
    unsafe {
        let head = PER_THREAD_QUEUE;
        if head.is_null() {
            return None;
        }

        let node = head as *mut usize;
        let next = node.read() as *mut u8;
        let size = node.add(1).read();
        PER_THREAD_QUEUE = next;
        PER_THREAD_QUEUE_OBJECTS = PER_THREAD_QUEUE_OBJECTS.saturating_sub(1);
        PER_THREAD_QUEUE_BYTES = PER_THREAD_QUEUE_BYTES.saturating_sub(size);
        Some((head, size))
    }
}

/// Drop all queue links without touching the queued memory itself.
pub fn clear_per_thread_queue() {
    unsafe {
        PER_THREAD_QUEUE = null_mut();
        PER_THREAD_QUEUE_OBJECTS = 0;
        PER_THREAD_QUEUE_BYTES = 0;
    }
}

/// Return queue occupancy for tests and runtime diagnostics.
pub fn per_thread_queue_snapshot() -> PerThreadQueueSnapshot {
    unsafe {
        PerThreadQueueSnapshot {
            objects: PER_THREAD_QUEUE_OBJECTS,
            bytes: PER_THREAD_QUEUE_BYTES,
            max_objects: MAX_PER_THREAD_QUEUE_OBJECTS,
            max_bytes: MAX_PER_THREAD_QUEUE_BYTES,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn try_push_per_thread_queue(ptr: *mut u8, size: usize) -> bool {
        unsafe { super::try_push_per_thread_queue(ptr, size) }
    }

    fn push_per_thread_queue(ptr: *mut u8, size: usize) {
        unsafe { super::push_per_thread_queue(ptr, size) }
    }

    fn pop_per_thread_queue() -> Option<(*mut u8, usize)> {
        unsafe { super::pop_per_thread_queue() }
    }

    #[test]
    fn push_pop_lifo() {
        clear_per_thread_queue();
        let mut a = [0usize; WORDS_IN_NODE];
        let mut b = [0usize; WORDS_IN_NODE];
        push_per_thread_queue(a.as_mut_ptr() as *mut u8, core::mem::size_of_val(&a));
        push_per_thread_queue(b.as_mut_ptr() as *mut u8, core::mem::size_of_val(&b));
        assert_eq!(pop_per_thread_queue().unwrap().0, b.as_mut_ptr() as *mut u8);
        assert_eq!(pop_per_thread_queue().unwrap().0, a.as_mut_ptr() as *mut u8);
        assert!(pop_per_thread_queue().is_none());
    }

    #[test]
    fn rejects_unaligned_storage() {
        clear_per_thread_queue();
        let mut storage = [0u8; MIN_QUEUE_OBJECT_SIZE + 1];
        let unaligned = storage.as_mut_ptr().wrapping_add(1);
        if (unaligned as usize) % core::mem::align_of::<usize>() == 0 {
            return;
        }
        push_per_thread_queue(unaligned, MIN_QUEUE_OBJECT_SIZE);
        assert!(pop_per_thread_queue().is_none());
    }

    #[test]
    fn try_push_rejects_when_byte_budget_would_grow() {
        clear_per_thread_queue();
        let mut first = [0usize; MIN_QUEUE_OBJECT_SIZE / size_of::<usize>()];
        let mut too_large = [0usize; (MAX_PER_THREAD_QUEUE_BYTES / size_of::<usize>()) + 1];

        assert!(try_push_per_thread_queue(
            first.as_mut_ptr() as *mut u8,
            core::mem::size_of_val(&first)
        ));
        assert!(!try_push_per_thread_queue(
            too_large.as_mut_ptr() as *mut u8,
            core::mem::size_of_val(&too_large)
        ));

        let snapshot = per_thread_queue_snapshot();
        assert_eq!(snapshot.objects, 1);
        assert_eq!(snapshot.bytes, core::mem::size_of_val(&first));
        assert_eq!(
            pop_per_thread_queue().map(|(ptr, _)| ptr),
            Some(first.as_mut_ptr() as *mut u8)
        );
        assert!(pop_per_thread_queue().is_none());
    }

    #[test]
    fn try_push_rejects_when_object_budget_is_full() {
        clear_per_thread_queue();
        let mut storage = [[0usize; WORDS_IN_NODE]; MAX_PER_THREAD_QUEUE_OBJECTS + 1];
        let object_size = core::mem::size_of_val(&storage[0]);

        for entry in storage.iter_mut().take(MAX_PER_THREAD_QUEUE_OBJECTS) {
            assert!(try_push_per_thread_queue(
                entry.as_mut_ptr() as *mut u8,
                object_size
            ));
        }

        let snapshot = per_thread_queue_snapshot();
        assert_eq!(snapshot.objects, MAX_PER_THREAD_QUEUE_OBJECTS);
        assert_eq!(snapshot.bytes, MAX_PER_THREAD_QUEUE_OBJECTS * object_size);

        let rejected = &mut storage[MAX_PER_THREAD_QUEUE_OBJECTS];
        assert!(!try_push_per_thread_queue(
            rejected.as_mut_ptr() as *mut u8,
            object_size
        ));
        assert_eq!(per_thread_queue_snapshot(), snapshot);

        for entry in storage[..MAX_PER_THREAD_QUEUE_OBJECTS].iter_mut().rev() {
            assert_eq!(
                pop_per_thread_queue().map(|(ptr, size)| (ptr, size)),
                Some((entry.as_mut_ptr() as *mut u8, object_size))
            );
        }
        assert!(pop_per_thread_queue().is_none());
    }

    #[test]
    fn queue_snapshot_tracks_pop_and_clear() {
        clear_per_thread_queue();
        let mut a = [0usize; WORDS_IN_NODE];
        let mut b = [0usize; WORDS_IN_NODE];
        let size_a = core::mem::size_of_val(&a);
        let size_b = core::mem::size_of_val(&b);

        assert!(try_push_per_thread_queue(a.as_mut_ptr() as *mut u8, size_a));
        assert!(try_push_per_thread_queue(b.as_mut_ptr() as *mut u8, size_b));
        let snapshot = per_thread_queue_snapshot();
        assert_eq!(snapshot.objects, 2);
        assert_eq!(snapshot.bytes, size_a + size_b);

        assert_eq!(pop_per_thread_queue().unwrap().0, b.as_mut_ptr() as *mut u8);
        let snapshot = per_thread_queue_snapshot();
        assert_eq!(snapshot.objects, 1);
        assert_eq!(snapshot.bytes, size_a);

        clear_per_thread_queue();
        let snapshot = per_thread_queue_snapshot();
        assert_eq!(snapshot.objects, 0);
        assert_eq!(snapshot.bytes, 0);
    }
}
