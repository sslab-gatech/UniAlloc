use crate::mpmc::{Q512, Q64};
#[cfg(target_os = "linux")]
use crate::pal::thread::linux::thread;
use core::ptr;
use core::sync::atomic::AtomicPtr;
use core::time::Duration;

const initial_wakeup_time: u64 = 1000; // microsecs

static GLOBAL_BG_THREAD: AtomicPtr<Q512<usize>> = AtomicPtr::<Q512<usize>>::new(ptr::null_mut());
// static WORKER_QUEUE:

struct Task {}

// currently it is linux only
pub fn start_background_thread() {
    let wakeup_time = initial_wakeup_time;

    loop {
        thread::sleep(Duration::from_micros(wakeup_time));
    }
}

#[cfg(test)]
mod tests {
    use crate::pal::thread::linux::thread;

    #[test]
    fn sanity() {
        let computation = thread::spawn(|| {
            // Some expensive computation.
            42
        });

        let result = computation.join().unwrap();
        assert_eq!(result, 42);
    }
}
