#[cfg(not(feature = "fixed_heap"))]
use crate::pal::sync::general_lock::{
    dynamic_initialize, lock, try_lock, unlock, OsLock, STATIC_INITIALIZER, SUPPORT_STATIC_INIT,
};
use core::cell::UnsafeCell;
use core::ops::{Deref, DerefMut, Drop};
use core::ptr;

pub trait Lockcell<T> {
    type Target;

    fn new(user_data: T) -> Self;
    fn lock(&self) -> Self::Target;
}

#[repr(align(8))]
pub struct PthreadMutex<T: ?Sized> {
    lock: UnsafeCell<OsLock>,
    data: UnsafeCell<T>,
}

pub struct PthreadMutexGuard<'a, T: ?Sized + 'a> {
    lock: &'a mut OsLock,
    data: &'a mut T,
}

unsafe impl<T: ?Sized + Send> Send for PthreadMutex<T> {}
unsafe impl<T: ?Sized + Send> Sync for PthreadMutex<T> {}

impl<T> PthreadMutex<T> {
    pub const fn new(user_data: T) -> Self {
        let lock = STATIC_INITIALIZER;
        Self {
            lock: UnsafeCell::new(lock),
            data: UnsafeCell::new(user_data),
        }
    }

    pub fn get_mut(&mut self) -> &mut T {
        unsafe { &mut *self.data.get() }
    }

    pub fn lock(&self) -> PthreadMutexGuard<'_, T> {
        unsafe {
            lock(self.lock.get());
        }
        PthreadMutexGuard {
            lock: unsafe { &mut *self.lock.get() },
            data: unsafe { &mut *self.data.get() },
        }
    }

    pub fn try_lock(&self) -> Option<PthreadMutexGuard<'_, T>> {
        if unsafe { try_lock(self.lock.get()) } {
            Some(PthreadMutexGuard {
                lock: unsafe { &mut *self.lock.get() },
                data: unsafe { &mut *self.data.get() },
            })
        } else {
            None
        }
    }
}

impl<'a, T: ?Sized> PthreadMutexGuard<'a, T> {
    pub(crate) fn get_mut(&mut self) -> &mut T {
        self.data
    }
}

impl<'a, T: ?Sized> Deref for PthreadMutexGuard<'a, T> {
    type Target = T;
    fn deref(&self) -> &T {
        &*self.data
    }
}

impl<'a, T: ?Sized> DerefMut for PthreadMutexGuard<'a, T> {
    fn deref_mut(&mut self) -> &mut T {
        &mut *self.data
    }
}

impl<'a, T: ?Sized> Drop for PthreadMutexGuard<'a, T> {
    fn drop(&mut self) {
        unsafe {
            unlock(self.lock);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pthread_mutex_try_lock_reports_busy_and_guard_drop_releases() {
        let mutex = PthreadMutex::new(7usize);

        let guard = mutex.lock();
        assert!(
            mutex.try_lock().is_none(),
            "try_lock must not block or re-enter while the mutex is held"
        );
        drop(guard);

        {
            let mut try_guard = mutex
                .try_lock()
                .expect("dropped guard should release the OS mutex");
            *try_guard += 1;
        }

        let final_guard = mutex.lock();
        assert_eq!(*final_guard, 8);
    }
}
