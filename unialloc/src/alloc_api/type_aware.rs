#[thread_local]
pub static mut PER_THREAD_QUEUE: *mut u8 = core::ptr::null_mut();

pub fn push_per_thread_queue(ptr: *mut u8, size: usize) {
    let ptr = ptr as *mut usize;
    unsafe { ptr.write(size) };
}

pub fn pop_per_thread_queue() -> Option<(*mut u8, usize)> {
    todo!()
}
