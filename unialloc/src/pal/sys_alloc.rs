//! underlying system allocator
use core::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use core::ptr::NonNull;
use core::result::Result;
use core::slice;

/// A builder to configure the page heap allocator
pub struct PageHeapBuilder {
    read: bool,
    write: bool,
    exec: bool,
}

impl PageHeapBuilder {
    pub fn build(&self) -> PageHeap {
        PageHeap {
            read: self.read,
            write: self.write,
            exec: self.exec,
        }
    }
}

impl Default for PageHeapBuilder {
    fn default() -> PageHeapBuilder {
        PageHeapBuilder {
            read: true,
            write: true,
            exec: false,
        }
    }
}

/// Page Heap allocator
/// An abstraction for underlying page allocator (e.g., mmap, get_free_pages)
pub struct PageHeap {
    read: bool,
    write: bool,
    exec: bool,
}

impl Default for PageHeap {
    fn default() -> PageHeap {
        PageHeapBuilder::default().build()
    }
}

extern crate std;
use std::thread::spawn;

/// # Safety
///
/// safe if the size is valid
#[cfg(unix)]
pub unsafe fn mmap(req: usize, prot: i32) -> *mut u8 {
    libc::mmap(
        core::ptr::null_mut(),
        req,
        prot,
        libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
        -1,
        0,
    ) as *mut u8
}

pub unsafe fn mmap_huge(req: usize, prot: i32) -> *mut u8 {
    libc::mmap(
        core::ptr::null_mut(),
        req,
        prot,
        libc::MAP_PRIVATE | libc::MAP_ANONYMOUS | libc::MAP_HUGETLB | libc::MAP_HUGE_2MB,
        -1,
        0,
    ) as *mut u8
}

/// # Safety
///
/// safe if the size is valid
#[cfg(target_os = "windows")]
pub unsafe fn mmap(req: usize, prot: u32) -> *mut u8 {
    use winapi::um::memoryapi::VirtualAlloc;
    use winapi::um::winnt::MEM_COMMIT;
    use winapi::um::winnt::MEM_RESERVE;

    VirtualAlloc(core::ptr::null_mut(), req, MEM_RESERVE | MEM_COMMIT, prot) as *mut u8
}

/// # Safety
///
/// safe if the size is valid
#[cfg(unix)]
pub unsafe fn munmap(ptr: *mut u8, size: usize) {
    libc::munmap(ptr as *mut libc::c_void, size);
}

/// # Safety
///
/// safe if the size is valid
#[cfg(target_os = "windows")]
pub unsafe fn munmap(ptr: *mut u8, _size: usize) {
    use winapi::um::memoryapi::VirtualFree;
    use winapi::um::winnt::MEM_RELEASE;
    VirtualFree(ptr as *mut _, 0, MEM_RELEASE);
}

mod blog_os_heap {
    pub unsafe fn mmap(req: usize, _prot: u32) -> *mut u8 {
        // https://github.com/phil-opp/blog_os/blob/post-12/src/allocator.rs#L16
        // blog_os has a 100K heap
        assert!(req <= 100 * 1024);
        0x_4444_4444_0000 as *mut u8
    }
}

mod redox_heap {
    // https://gitlab.redox-os.org/redox-os/kernel/-/blob/master/src/arch/x86_64/consts.rs#L19
    pub const PML4_SIZE: usize = 0x0000_0080_0000_0000;
    pub const PML4_MASK: usize = 0x0000_ff80_0000_0000;

    /// Offset of recursive paging
    pub const RECURSIVE_PAGE_OFFSET: usize = (-(PML4_SIZE as isize)) as usize;
    pub const RECURSIVE_PAGE_PML4: usize = (RECURSIVE_PAGE_OFFSET & PML4_MASK) / PML4_SIZE;

    /// Offset of kernel
    pub const KERNEL_OFFSET: usize = 0xFFFF_8000_0000_0000; //TODO: better calculation
    pub const KERNEL_PML4: usize = (KERNEL_OFFSET & PML4_MASK) / PML4_SIZE;

    /// Offset to kernel heap
    pub const KERNEL_HEAP_OFFSET: usize = RECURSIVE_PAGE_OFFSET - PML4_SIZE;
    pub const KERNEL_HEAP_PML4: usize = (KERNEL_HEAP_OFFSET & PML4_MASK) / PML4_SIZE;
    /// Size of kernel heap
    pub const KERNEL_HEAP_SIZE: usize = 1024 * 1024; // 1 MB

    pub unsafe fn mmap(req: usize, _prot: u32) -> *mut u8 {
        assert!(req <= KERNEL_HEAP_SIZE);
        KERNEL_HEAP_OFFSET as *mut u8
    }
}

unsafe impl GlobalAlloc for PageHeap {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        debug_assert_eq!(layout.size() % 0x1000, 0, "size: 0x{:x}", layout.size());
        mmap(
            layout.size(),
            prots::get_prot(self.read, self.write, self.exec),
        )
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        debug_assert_eq!(layout.size() % 0x1000, 0);
        debug_assert_eq!(ptr as usize % 0x1000, 0);
        munmap(ptr, layout.size());
    }
}

unsafe impl Allocator for PageHeap {
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        unsafe {
            let ptr = mmap(
                layout.size(),
                prots::get_prot(self.read, self.write, self.exec),
            );
            Ok(NonNull::new(slice::from_raw_parts_mut(ptr, layout.size()))
                .expect("MMAP_ALLOC cannot allocate"))
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        munmap(ptr.as_ptr(), layout.size());
    }
}

// pub fn madvise_willneed(ptr: *mut u8, len: usize) {
//     unsafe {
//         libc::madvise(ptr as *mut libc::c_void, len, libc::MADV_WILLNEED);
//     }
// }

// pub fn madvise_random(ptr: *mut u8, len: usize) {
//     unsafe {
//         libc::madvise(ptr as *mut libc::c_void, len, libc::MADV_RANDOM);
//     }
// }

// https://docs.rs/mmap-alloc/0.2.0/src/mmap_alloc/lib.rs.html
pub mod prots {
    #[cfg(unix)]
    pub use self::unix::*;
    #[cfg(unix)]
    pub type Prot = i32;
    #[cfg(windows)]
    pub use self::windows::*;
    #[cfg(windows)]
    pub type Prot = u32;

    pub fn get_prot(read: bool, write: bool, exec: bool) -> Prot {
        match (read, write, exec) {
            (false, false, false) => PROT_NONE,
            (true, false, false) => PROT_READ,
            (false, true, false) => PROT_WRITE,
            (false, false, true) => PROT_EXEC,
            (true, true, false) => PROT_READ_WRITE,
            (true, false, true) => PROT_READ_EXEC,
            (false, true, true) => PROT_WRITE_EXEC,
            (true, true, true) => PROT_READ_WRITE_EXEC,
        }
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    mod unix {
        // NOTE: On some platforms, libc::PROT_WRITE may imply libc::PROT_READ, and libc::PROT_READ
        // may imply libc::PROT_EXEC.
        extern crate libc;
        pub const PROT_NONE: i32 = libc::PROT_NONE;
        pub const PROT_READ: i32 = libc::PROT_READ;
        pub const PROT_WRITE: i32 = libc::PROT_WRITE;
        pub const PROT_EXEC: i32 = libc::PROT_EXEC;
        pub const PROT_READ_WRITE: i32 = libc::PROT_READ | libc::PROT_WRITE;
        pub const PROT_READ_EXEC: i32 = libc::PROT_READ | libc::PROT_EXEC;
        pub const PROT_WRITE_EXEC: i32 = libc::PROT_WRITE | libc::PROT_EXEC;
        pub const PROT_READ_WRITE_EXEC: i32 = libc::PROT_READ | libc::PROT_WRITE | libc::PROT_EXEC;
    }

    #[cfg(windows)]
    mod windows {
        extern crate winapi;
        use self::winapi::um::winnt;
        pub const PROT_NONE: u32 = winnt::PAGE_NOACCESS;
        pub const PROT_READ: u32 = winnt::PAGE_READONLY;
        // windows doesn't have a write-only permission, so write implies read
        pub const PROT_WRITE: u32 = winnt::PAGE_READWRITE;
        pub const PROT_EXEC: u32 = winnt::PAGE_EXECUTE;
        pub const PROT_READ_WRITE: u32 = winnt::PAGE_READWRITE;
        pub const PROT_READ_EXEC: u32 = winnt::PAGE_EXECUTE_READ;
        // windows doesn't have a write/exec permission, so write/exec implies read/write/exec
        pub const PROT_WRITE_EXEC: u32 = winnt::PAGE_EXECUTE_READWRITE;
        pub const PROT_READ_WRITE_EXEC: u32 = winnt::PAGE_EXECUTE_READWRITE;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn it_works() {
        let layout = Layout::from_size_align(0x1000, 8).expect("It does not work");
        let page_allocator = PageHeap::default();
        unsafe {
            page_allocator.alloc(layout);
        }
    }
}
