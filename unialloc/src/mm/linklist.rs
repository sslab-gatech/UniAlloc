#[cfg(target_arch = "aarch64")]
use crate::pal::arch::pac::*;
use crate::*;

#[derive(Clone, Copy)]
pub struct Linklist {
    pub link: usize,
    pub length: usize,
}

impl Linklist {
    pub const fn new() -> Self {
        Self { link: 0, length: 0 }
    }

    /// Security
    ///
    /// This API lacks of pointer authentication
    pub fn push_unchecked(&mut self, ptr: *mut u8) {
        if ptr as usize == 0 {
            debug_assert_ne!(ptr as usize, 0);
        }
        let target = ptr as *mut usize;
        let current = self.link;
        unsafe {
            *target = current;
        }

        self.link = target as usize;
        self.length += 1;
    }

    // /// Security
    // ///
    // /// This API lacks of pointer authentication
    // pub fn pop_unchecked(&mut self) -> *mut u8 {
    //     if self.link == 0 {
    //         debug_assert_eq!(self.length, 0);
    //         return core::ptr::null_mut::<u8>();
    //     }
    //
    //     let result = self.link;
    //     let next = unsafe { *(result as *const usize) };
    //     self.link = next;
    //     // if next != 0 {
    //     //     unsafe {
    //     //         core::intrinsics::prefetch_read_data(next as *const usize, 3);
    //     //     }
    //     // }
    //     self.length -= 1;
    //     result as *mut u8
    // }

    pub fn pop_unchecked_aligned(&mut self, align: usize) -> *mut u8 {
        if self.length == 0 {
            debug_assert_eq!(self.length, 0);
            return core::ptr::null_mut::<u8>();
        }

        let result = self.link;
        assert_ne!(result, 0);
        if result & (align - 1) == 0 {
            let next = unsafe { *(result as *const usize) };
            self.link = next;
            // if next != 0 {
            //     unsafe {
            //         core::intrinsics::prefetch_read_data(next as *const usize, 3);
            //     }
            // }
            self.length -= 1;
            result as *mut u8
        } else {
            let mut pre = result;
            let mut now = unsafe { *(pre as *const usize) };
            while now != 0 {
                if now & (align - 1) == 0 {
                    unsafe { *(pre as *mut usize) = *(now as *mut usize) };
                    self.length -= 1;
                    break;
                } else {
                    pre = now;
                    now = unsafe { *(pre as *const usize) };
                }
            }

            now as *mut u8
        }
    }

    pub fn length(&self) -> usize {
        self.length
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_arch = "aarch64")]
    #[test]
    fn freelist_push_pop_test() {
        let mut list = Linklist::new();
        println!("list addr: 0x{:x}", &list as *const _ as usize);
        let mock_chunk = 0xdeadbeefusize;
        let mock_chunk2 = 0xcafeusize;
        list.push(&mock_chunk as *const usize as *mut u8);
        assert_eq!(mock_chunk, 0);
        list.push(&mock_chunk2 as *const usize as *mut u8);
        assert_eq!(list.pop() as usize, &mock_chunk2 as *const usize as usize);
        assert_eq!(list.pop() as usize, &mock_chunk as *const usize as usize);
        assert_eq!(list.pop() as usize, 0usize);
    }

    #[cfg(target_arch = "aarch64")]
    #[test]
    #[should_panic]
    fn wrong_pac_test() {
        let mut list = Linklist::new();
        let mock_chunk = 0xdeadbeefusize;
        let mut mock_chunk2 = 0xdeadbeefusize;
        list.push(&mock_chunk as *const usize as *mut u8);
        list.push(&mock_chunk2 as *const usize as *mut u8);
        mock_chunk2 = pacib(&mock_chunk as *const _ as usize, 0);
        list.pop();
        list.pop();
    }

    #[test]
    fn struct_addr_test() {
        let list = Linklist::new();
        println!("hello: 0x{:x}", &list as *const _ as usize);
    }
}
