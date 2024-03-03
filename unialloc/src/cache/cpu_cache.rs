//! Per-CPU cache
//!
//! For linux-userspace, we utilize the restartable sequence system call for
//! maintaining Per-CPU cache
//!
//! For linux-kernelspace, we directly utilize the Per-CPU variable along with
//! manipulation of preempt (e.g., preempt_disable() and preempt_enable)

use crate::collections::concurrent::PerCpuArray;
use crate::error::{AllocError, Result};
use crate::pal::os::rseq::*;
use crate::prelude::*;
use crate::zone::ZoneAllocator;
use crate::zone::GLOBAL_ZONE;
use crate::*;
use alloc::alloc::{Allocator, GlobalAlloc, Layout};
use buddy_system::BuddySystemAllocator as Arena;
use core::mem;
use core::ptr::{self, write_bytes, NonNull};
use core::slice;
use core::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};

const NUM_CLASSES: usize = 63;
const CCACHE_SIZE: usize = 512;
const SHIFT: usize = 18;
const SLAB_SIZE: usize = 1usize << SHIFT;
const HDR_SIZE: usize = mem::size_of::<[AtomicUsize; NUM_CLASSES]>();
#[thread_local]
static mut CWND: usize = 64;

/// Initializes a non-copy array utilizing `MaybeUninit`.
///
/// # Examples
///
/// ```rust,no_run
/// array_init!{AtomicUsize, NUM_CLASSES, AtomicUsize::new(0usize)}
/// ```
macro_rules! array_init {
    ($ty:ty, $len:expr, $constructor:expr) => {{
        // Create an uninitialized array of `MaybeUninit`. The `assume_init` is
        // safe because the type we are claiming to have initialized here is a
        // bunch of `MaybeUninit`s, which do not require initialization.
        let mut data: [core::mem::MaybeUninit<$ty>; $len] =
            unsafe { core::mem::MaybeUninit::uninit().assume_init() };

        // Dropping a `MaybeUninit` does nothing. Thus using raw pointer
        // assignment instead of `ptr::write` does not cause the old
        // uninitialized value to be dropped. Also if there is a panic during
        // this loop, we have a memory leak, but there is no memory safety
        // issue.
        for elem in &mut data[..] {
            *elem = core::mem::MaybeUninit::new($constructor);
        }

        // Everything is initialized. Transmute the array to the
        // initialized type.
        unsafe { core::mem::transmute::<_, [$ty; $len]>(data) }
    }};
}

/// # Note
///
/// 1. All of the offsets are relative to the start of slab, including the headers.
///
/// 2. We do not use `slabs[begin]` because we will prefetch the next one while
/// allocating, so `slabs[begin]` always pointers to itself to guarantee the
/// prefetch instruction fast enough.
#[repr(C)]
struct Header {
    current: u16,
    end_copy: u16,
    begin: u16,
    end: u16,
}

impl Header {
    pub fn new(current: usize, end_copy: usize, begin: usize, end: usize) -> Self {
        Self {
            current: current as u16,
            end_copy: end_copy as u16,
            begin: begin as u16,
            end: end as u16,
        }
    }

    pub fn is_locked(&self) -> bool {
        self.begin == 0xffffu16
    }

    pub fn lock(&mut self) {
        self.begin = 0xffffu16;
        self.end = 0;
    }

    pub fn locked_header() -> Self {
        Self {
            current: 0,
            end_copy: 0,
            begin: 0xffffu16,
            end: 0,
        }
    }

    pub fn inner_values(&self) -> (usize, usize, usize, usize) {
        (
            self.current.into(),
            self.end_copy.into(),
            self.begin.into(),
            self.end.into(),
        )
    }
}

impl core::convert::From<usize> for Header {
    fn from(hdr: usize) -> Self {
        let current = hdr & 0xffff;
        let end_copy = (hdr >> 16) & 0xffff;
        let begin = (hdr >> 32) & 0xffff;
        let end = (hdr >> 48) & 0xffff;
        Self {
            current: current as u16,
            end_copy: end_copy as u16,
            begin: begin as u16,
            end: end as u16,
        }
    }
}

impl core::convert::From<Header> for usize {
    fn from(hdr: Header) -> usize {
        let mut res: usize = 0;
        res += (hdr.end as usize) << 48;
        res += (hdr.begin as usize) << 32;
        res += (hdr.end_copy as usize) << 16;
        res += hdr.current as usize;
        res
    }
}

/// A Per-CPU slab for one CPU
/// We gonna have an array of PerCPUSlabs with NCPU members
/// size: 1 << SHIFT
#[repr(C, align(16))]
struct CPUSlab {
    header: [AtomicUsize; NUM_CLASSES],
    mem: [usize; (SLAB_SIZE - HDR_SIZE) / mem::size_of::<usize>()],
}

impl CPUSlab {
    pub fn new() -> Self {
        Self {
            header: array_init! {AtomicUsize, NUM_CLASSES, AtomicUsize::new(0usize)},
            mem: [0usize; (SLAB_SIZE - HDR_SIZE) / mem::size_of::<usize>()],
        }
    }
}

#[repr(C, align(16))]
pub struct PerCpuSlabs {
    slabs: [CPUSlab; NCPU],
}

impl PerCpuSlabs {
    pub fn new() -> Self {
        Self {
            slabs: array_init! {CPUSlab, NCPU, CPUSlab::new()},
        }
    }

    pub fn init(&self) {
        for i in 0..NCPU {
            self.init_cpu(i);
        }
    }

    #[inline]
    pub fn get_capacity(_cl: usize) -> usize {
        500
    }

    /// init_cpu will be called upon updating capacity
    pub fn init_cpu(&self, cpu: usize) {
        // 1. stop concurrent mutation
        for cl in 0..NUM_CLASSES {
            // check whether the current size class is locked
            let mut hdr: Header = self.slabs[cpu].header[cl].load(Ordering::Relaxed).into();
            if hdr.is_locked() {
                panic!("CPU[{}] is locked", cpu);
            }
            // locking the current size class
            hdr.lock();
            self.slabs[cpu].header[cl].store(hdr.into(), Ordering::Relaxed);
        }

        // calculate the header's size
        let slabs = &self.slabs as *const _ as usize;
        let slab_addr = slabs + (cpu << SHIFT);
        let mut byte_used = mem::size_of::<[AtomicUsize; NUM_CLASSES]>();

        // 2. initialize prefetch targets
        for cl in 0..NUM_CLASSES {
            let current_addr = slab_addr + byte_used;
            let cap = Self::get_capacity(cl);

            // set up prefetch target
            if cap != 0 {
                // Safety: The first element will not be used, and it is only
                // used as a prefetch target
                let target: *mut usize = current_addr as *const usize as *mut usize;
                unsafe {
                    *target = current_addr;
                }
            }

            let offset = byte_used / mem::size_of::<*mut u8>();
            byte_used += (cap) * mem::size_of::<*mut u8>();

            // create a new header
            //
            // TODO: make it lazy initialized
            // The reason why we want to set current == begin == end is that
            // we want some size classes that are frequently used can have
            // a bigger capacity.
            let current = offset;
            let begin = offset;
            let end_copy = offset + cap;
            let end = offset + cap;

            // update header
            let hdr = Header::new(current, end_copy, begin, end);
            // 3. allowing access the current cache
            self.slabs[cpu].header[cl].store(hdr.into(), Ordering::Relaxed);
        }
        assert!(
            byte_used <= (1 << SHIFT),
            "[INIT_PER_CPU]: size overflowed! byte_used: 0x{:x}, max: 0x{:x}, header: 0x{:x}",
            byte_used,
            1 << SHIFT,
            mem::size_of::<[AtomicUsize; NUM_CLASSES]>(),
        );
    }

    #[cfg(not(rseq))]
    pub fn pop(&self, _cl: usize) -> Result<NonNull<u8>, usize> {
        panic!("rseq on non-linux platform is not supported yet");
    }

    #[cfg(not(rseq))]
    pub fn push(&self, _ptr: NonNull<u8>, _cl: usize) -> Result<(), usize> {
        panic!("rseq on non-linux platform is not supported yet");
    }

    #[cfg(rseq)]
    #[inline(never)]
    pub fn pop(&self, cl: usize) -> core::result::Result<NonNull<u8>, usize> {
        let scratch: usize;
        let before: usize;
        let current: usize; //after
        let result: *mut u8;
        let slab_addr = &self.slabs as *const _ as usize;

        unsafe {
            asm!(
                // building the rseq cs table
                ".pushsection _table_pop, \"aw?\"",
                ".balign 32",
                // rseq_cs table
                "1338:",
                ".long 0, 0",
                // actually, before writing into the rseq_cs,
                // the atomicity won't be maintained
                //
                // start_ip, commit_ip, abort_ip
                ".quad 4f, 5f - 4f, 2f",
                ".popsection",
                // abort trampoline
                ".pushsection _failure_pop, \"ax\"",
                // disassembler friendly signature
                ".byte 0xf, 0x1f, 0x5",
                ".long {signature}",
                // abort handler (restart)
                "2:",
                "jmp 3f", // restart
                ".popsection",
                // Prepare:
                "3:",
                "lea {scratch}, [rip + 1338b]",
                // rseq layout (align 32)
                // 0: cpu_id_start
                // 4: cpu_id
                // 8: rseq_cs pointer
                // 16: flags
                //
                // set the rseq_cs pointer
                "mov qword ptr [{rseq} + {cs_offset}], {scratch}",
                // TX start:
                "4:",
                // load cpu id
                // NOTE: it does not matter whether we use cpu_id_start or cpu_id
                // because the cpu_id will only be loaded inside the transaction
                //
                // scratch = __rseq_abi.cpu_id
                "movzx {scratch}, word ptr [{rseq} + {cpu_id_offset}]",
                // get header offset
                // NOTE: sizeof(PerCPUSlabs): 1 << SHIFT
                // scratch = slabs + scratch (locate the current CPU region)
                "shl {scratch}, {shift}",
                "lea {scratch}, [{scratch} +{slabs}]",
                // current = scratch->header[cl].current
                "movzx {current}, word ptr [{scratch} + {cl}*8]",
                "mov {before}, {current}",
                // current <= begin?
                "cmp {current:x}, word ptr [{scratch} + {cl}*8 + 4]",
                // jmp to underflow handler
                "jbe 5f",
                "mov {result}, qword ptr [{scratch} + {current}*8 - 16]",
                "prefetcht0 [{result}]",
                "mov {result}, qword ptr [{scratch} + {current}*8 - 8]",
                // current--
                "lea {current}, [{current}-1]",
                // update current
                "mov word ptr [{scratch} + {cl}*8], {current:x}",
                // commit
                "5:",
                signature = const 0x53053053,
                rseq = in(reg) &RSEQ_ABI as *const _ as usize,
                cs_offset = const 8,
                cpu_id_offset = const 4,
                shift = const SHIFT,
                slabs = in(reg) slab_addr,
                cl = in(reg) cl, // size class
                before = out(reg) before,
                result = out(reg) result, // the result pointer
                current = out(reg) current, // clobbered
                scratch = out(reg) scratch,
            );
        }

        // We did not get a proper pointer, which means the underflow
        // occurred. In this way, we need to either extend the capacity
        // of the current CPU cache or allocate a chunk from central freelist.
        if current + 1 != before {
            // Underflow!

            // Calculate CPU ID
            //
            // Different from the `push` operation,
            // we do not want to wrongly handle another cpu cache
            let offset = scratch - slab_addr;
            let cpu_id = offset / SLAB_SIZE;
            debug_assert!(cpu_id <= NCPU);
            Err(cpu_id)
        } else {
            // println!("0x{:x}", result as usize);
            // println!("cur: {}", current);
            Ok(NonNull::new(result).expect("NonNull pointer cannot be NULL!"))
        }
    }

    #[cfg(rseq)]
    pub fn push(&self, ptr: NonNull<u8>, cl: usize) -> core::result::Result<(), usize> {
        let r11: usize;
        let before: usize;
        let scratch: usize;
        let slab_addr = &self.slabs as *const _ as usize;
        unsafe {
            asm!(
                // building the rseq cs table
                ".pushsection _table_push, \"aw?\"",
                ".balign 32",
                // rseq_cs table
                "1337:",
                ".long 0",
                ".long 0",
                // actually, before writing into the rseq_cs,
                // the atomicity won't be maintained
                //
                // start_ip, commit_ip, abort_ip
                ".quad 4f",
                ".quad 5f - 4f",
                ".quad 2f",
                ".popsection",
                // abort trampoline
                ".pushsection _fail_push, \"ax\"",
                // disassembler friendly signature
                ".byte 0xf, 0x1f, 0x5",
                ".long {signature}",
                // abort handler (restart)
                "2:",
                // "call {abort}", //debug
                "jmp 3f", // restart
                ".popsection",
                // Prepare:
                //
                // r10: Scratch
                // r11: Current
                "3:",
                // r10 <- &cs_table
                "lea r10, [rip + 1337b]",
                // rseq layout (align 32)
                // 0: cpu_id_start
                // 4: cpu_id
                // 8: rseq_cs pointer
                // 16: flags
                //
                // set the rseq_cs pointer
                "mov qword ptr [{rseq} + {cs_offset}], r10",
                // TX start:
                "4:",
                // load cpu id
                // NOTE: it does not matter whether we use cpu_id_start or cpu_id
                // because the cpu_id will only be loaded inside the transaction
                //
                // r10 = __rseq_abi.cpu_id
                "movzx r10d, word ptr [{rseq} + {cpu_id_offset}]",
                // get header offset
                // NOTE: sizeof(PerCPUSlabs): 1 << SHIFT
                // scratch = slabs + scratch (locate the current CPU region)
                "shl r10, {shift}",
                // r10 = current headers
                "lea r10, [r10 + {slabs}]",
                // r11 = header.current because the `current` variable
                // is the word at the beginning of `header`
                //
                // cl * 8 is used to find the specific header
                "movzx r11, word ptr [r10 + {cl}*8]",
                "mov {before}, r11", // I use an extra register to check overflow
                // current >= end？
                "cmp r11w, word ptr [r10 + {cl}*8 + 6]",
                // jmp to overflow handler
                "jae 5f",
                // not overflow:
                // perform side effects
                //
                // [+] first store:
                // The side effect is allowed because we always retry
                // when the TX fails
                //
                // r11 is relative to current slab
                "mov qword ptr [r10 + r11*8], {item}",
                // current++
                "lea r11, [r11 + 1]",
                // [+] second store: update current
                // header->current = new_current
                "mov word ptr [r10 + {cl}*8], r11w",
                // commit
                "5:", // overflow handler
                // "movzx r10d, word ptr [r10 + {cl}*8 + 6]",
                signature = const 0x53053053,
                rseq = in(reg) &RSEQ_ABI as *const _ as usize,
                cs_offset = const 8,
                cpu_id_offset = const 4,
                shift = const SHIFT,
                slabs = in(reg) &self.slabs as *const _ as usize,
                cl = in(reg) cl, // size class index
                item = in(reg) ptr.as_ptr() as usize, // pointer
                before = out(reg) before,
                out("r10") scratch, // clobbered
                out("r11") r11, // current (after)
            );
        }
        if r11 == before + 1 {
            debug_assert_ne!(ptr.as_ptr() as usize, 0);
            Ok(())
        } else {
            // overflow and return cpu id
            let offset = scratch - slab_addr;
            let cpu_id = offset / SLAB_SIZE;
            Err(cpu_id)
        }
    }

    /// Refilling the CpuCache by allocating a batch of objects from central
    /// freelist
    ///
    /// Returns the next idx
    pub fn push_batch(
        &self,
        batch: &mut [usize],
        cl: usize,
        len: usize,
        start_idx: usize,
    ) -> usize {
        // n: start index
        //change, new impl will ret the ans as the end of the array
        let mut n = start_idx;
        while n < len {
            let ptr = unsafe { NonNull::new_unchecked(batch[n - start_idx] as *mut u8) };
            let res = self.push(ptr, cl);
            if res.is_ok() {
                n += 1;
            } else {
                break;
            }
        }
        n
    }

    /// Returns a batch of objects to central freelist
    pub fn pop_batch(&self, batch: &mut [usize], cl: usize, len: usize, start_idx: usize) -> usize {
        let mut n = start_idx;
        while n < len {
            let ptr = self.pop(cl);
            if let Ok(p) = ptr {
                batch[n] = p.as_ptr() as usize;
                n += 1;
            } else {
                break;
            }
        }
        n
    }

    fn get_cpu_id() -> usize {
        let id = cpu_id();
        assert!(id >= 0);
        id as usize
    }
}

impl PerCpuSlabs {
    pub fn allocate(&self, layout: Layout) -> Result<NonNull<u8>> {
        // Step 0: check and register the rseq
        register_current_thread_checked();

        // Step 1: get size class
        let cls = get_size_class(layout.size());
        let align = layout.align();

        if let SizeClass::Base(idx) = cls {
            // Step 2: try to allocate from cpu cache
            let result = self.pop(cls.index());

            // Step 3: check underflow
            if let Err(cpu) = result {
                // Underflow
                // There is no pointer in the requested cpu cache

                // refill the corresponding cpu cache
                if let Some(ptr) = self.refill_cache(cpu, idx) {
                    return Ok(NonNull::new(ptr as *mut _).expect("The refill process has error"));
                } else {
                    return Self::alloc_from_zone(layout.size(), align);
                }
            }

            // Step 4: check alignment
            let inner_ptr = result.expect("cannot get result");
            if (inner_ptr.as_ptr() as usize) & (layout.align() - 1) != 0 {
                // Stage 0: push the unaligned chunk back
                //
                // TODO: Refactoring the push and pop function for allowing
                // a error handler get passed into it.
                self.deallocate(inner_ptr, layout);

                return Self::alloc_from_zone(layout.size(), align);
            }

            return Ok(inner_ptr);
        } else if let SizeClass::Large(_) = cls {
            return Self::alloc_from_zone(layout.size(), align);
        }

        println!("{}", layout.size());

        Err(AllocError::EFATAL)
    }

    pub fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        // Step 0: check the register the rseq
        register_current_thread_checked();

        // Step 1: get size class
        let cls = get_size_class(layout.size());

        if let SizeClass::Base(idx) = cls {
            // Step 2: try to deallocate to the cpu c
            let result = self.push(ptr, idx);

            // Step 3: check overflow
            if let Err(cpu) = result {
                // Overflow!
                // TODO: we need to return a batch of chunk to the zone

                let evict_res = self.evict_cache(ptr, cpu, idx);
                if evict_res.is_none() {
                    Self::dealloc_to_zone(ptr, layout.size());
                }
            }
        } else if let SizeClass::Large(_) = cls {
            Self::dealloc_to_zone(ptr, layout.size());
        }
    }

    /// Refills the cache for specific cpu for size class cl
    ///
    /// The tricky part is that we cannot guarantee whether we are
    /// migrated to a different CPU
    ///
    /// Upon success, return the first one for immediate usage
    pub fn refill_cache(&self, cpu: usize, idx: usize) -> Option<usize> {
        let mut batch = [0usize; 512];
        let batch_size = unsafe { CWND };
        let n: usize;

        // We cannot precisely control which cache we are pushing
        if cpu_id() as usize == cpu {
            let ans = (*GLOBAL_ZONE).allocate_batch_from_slab(idx, &mut batch, batch_size);
            if ans.is_ok() {
                n = self.push_batch(&mut batch, idx, batch_size, 1);
                if n < batch_size {
                    (*GLOBAL_ZONE)
                        .deallocate_batch_to_slab(idx, &mut batch[n..], batch_size - n)
                        .expect("free to slab failed");
                }
                return Some(batch[0]);
            } else {
                return None;
            }
        }
        None
    }

    pub fn evict_cache(&self, ptr: NonNull<u8>, cpu: usize, idx: usize) -> Option<()> {
        let mut batch = [0usize; 512];
        let batch_size = unsafe { CWND };
        let n: usize;

        if cpu_id() as usize == cpu {
            batch[0] = ptr.as_ptr() as usize;
            n = self.pop_batch(&mut batch, idx, batch_size - 1, 1);
            (*GLOBAL_ZONE)
                .deallocate_batch_to_slab(idx, &mut batch, n)
                .expect("free to slab failed");
            Some(())
        } else {
            None
        }
    }

    /// A simple trampoline to zone
    #[inline]
    fn alloc_from_zone(sz: usize, align: usize) -> Result<NonNull<u8>> {
        (*GLOBAL_ZONE).allocate(sz, align)
    }

    /// A simple trampoline to zone
    #[inline]
    fn dealloc_to_zone(ptr: NonNull<u8>, sz: usize) {
        (*GLOBAL_ZONE).deallocate(ptr, sz);
    }
}

// atomic_static! {
//     pub static ref GLOBAL_CCACHE: PerCpuSlabs = {
//         let slab = Box::new_in(PerCpuSlabs::new(), GlobalBackend);
//         slab.init();
//         slab
//     };
// }

static GLOBAL_CCACHE_VALUE: core::sync::atomic::AtomicPtr<PerCpuSlabs> =
    core::sync::atomic::AtomicPtr::new(core::ptr::null_mut());

#[allow(non_camel_case_types)]
pub struct GLOBAL_CCACHE;

impl core::ops::Deref for GLOBAL_CCACHE {
    type Target = PerCpuSlabs;

    fn deref(&self) -> &PerCpuSlabs {
        extern crate alloc;
        use alloc::alloc::GlobalAlloc;
        use alloc::boxed::Box;

        let ptr: *mut PerCpuSlabs = GLOBAL_CCACHE_VALUE.load(core::sync::atomic::Ordering::Acquire);
        if !ptr.is_null() {
            return unsafe { ptr.as_ref().unwrap() };
        }

        // let boxed_ptr:Box<PerCpuSlabs, GlobalBackend> =
        //                 Box::new_in(
        //                     PerCpuSlabs::new()
        //                     , GlobalBackend);
        // boxed_ptr.init();
        // let init_ptr = Box::into_raw(boxed_ptr);

        let layout = alloc::alloc::Layout::new::<PerCpuSlabs>();
        let init_ptr = unsafe { GlobalBackend.alloc(layout) as *mut PerCpuSlabs };
        let slab: &PerCpuSlabs = unsafe { &*(init_ptr as *const _) };
        slab.init();

        if let Err(p) = GLOBAL_CCACHE_VALUE.compare_exchange(
            core::ptr::null_mut(),
            init_ptr,
            core::sync::atomic::Ordering::AcqRel,
            core::sync::atomic::Ordering::Relaxed,
        ) {
            if !p.is_null() {
                unsafe {
                    // Box::from_raw_in(init_ptr as *mut PerCpuSlabs, GlobalBackend);
                    GlobalBackend.dealloc(init_ptr as *mut u8, layout);
                    return p.as_ref().unwrap();
                }
            }
        }
        unsafe { init_ptr.as_ref().unwrap() }
    }
}

impl core::ops::DerefMut for GLOBAL_CCACHE {
    fn deref_mut(&mut self) -> &mut PerCpuSlabs {
        extern crate alloc;
        use alloc::alloc::GlobalAlloc;
        use alloc::boxed::Box;

        let ptr: *mut PerCpuSlabs = GLOBAL_CCACHE_VALUE.load(core::sync::atomic::Ordering::Acquire);
        if !ptr.is_null() {
            return unsafe { ptr.as_mut().unwrap() };
        }
        // let boxed_ptr: Box<PerCpuSlabs, GlobalBackend> =
        //     Box::new_in(PerCpuSlabs::new(), GlobalBackend);
        // boxed_ptr.init();
        // let init_ptr = Box::into_raw(boxed_ptr);

        let layout = alloc::alloc::Layout::new::<PerCpuSlabs>();
        let init_ptr = unsafe { GlobalBackend.alloc(layout) as *mut PerCpuSlabs };
        let slab: &PerCpuSlabs = unsafe { &*(init_ptr as *const _) };
        slab.init();

        if let Err(p) = GLOBAL_CCACHE_VALUE.compare_exchange(
            core::ptr::null_mut(),
            init_ptr,
            core::sync::atomic::Ordering::AcqRel,
            core::sync::atomic::Ordering::Relaxed,
        ) {
            if !p.is_null() {
                unsafe {
                    // Box::from_raw_in(init_ptr as *mut PerCpuSlabs, GlobalBackend);
                    GlobalBackend.dealloc(init_ptr as *mut u8, layout);
                    return p.as_mut().unwrap();
                }
            }
        }
        unsafe { init_ptr.as_mut().unwrap() }
    }
}

#[derive(Copy, Clone)]
pub struct CpuCache;

unsafe impl GlobalAlloc for CpuCache {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if let Ok(result) = (*GLOBAL_CCACHE).allocate(layout) {
            result.as_ptr()
        } else {
            ptr::null_mut()
        }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        let ptr = NonNull::new(ptr).expect("ptr is null!");
        (*GLOBAL_CCACHE).deallocate(ptr, layout);
    }
}

unsafe impl Allocator for CpuCache {
    fn allocate(
        &self,
        layout: Layout,
    ) -> core::result::Result<NonNull<[u8]>, alloc::alloc::AllocError> {
        unsafe {
            let p = self.alloc(layout);
            Ok(NonNull::new(slice::from_raw_parts_mut(p, layout.size()))
                .expect("CPU Cache cannot allocate"))
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        (*GLOBAL_CCACHE).deallocate(ptr, layout);
    }
}

#[cfg(all(test, rseq))]
mod tests {
    use super::*;
    use crate::*;
    extern crate std;
    use rand::{self, Rng};
    use std::thread;
    use std::time;
    use std::vec::Vec;

    #[test]
    fn size_tests() {
        assert_eq!(mem::size_of::<AtomicUsize>(), 8);
        assert_eq!(mem::size_of::<Header>(), 8);
        assert_eq!(mem::size_of::<CPUSlab>(), 1 << SHIFT);
        println!("mem size: 0x{:x}", SLAB_SIZE - HDR_SIZE);
        println!(
            "per-size cap: 0x{:x}",
            (SLAB_SIZE - HDR_SIZE) / 8 / NUM_CLASSES
        );
    }

    #[test]
    fn header_size_test() {
        let hdr = Header::new(0x12, 0x23, 0x45, 0x67);
        assert_eq!(0x0067004500230012usize, hdr.into());
    }

    #[test]
    fn get_cpu_slab_test() {
        register_current_thread_checked();
    }

    // #[test]
    // fn cpucache_push_test() {
    //     let layout = Layout::from_size_align(2usize.pow(13), 8).expect("It does not work");
    //     let ptr0 = unsafe { Arena.alloc(layout) };
    //     let ptr = NonNull::new(ptr0).expect("cannot create");
    //     let before = ptr0 as usize;

    //     register_current_thread_checked();
    //     let res = (*GLOBAL_CCACHE).push(ptr, 49);
    //     assert!(res.is_ok(), "Overflow occurred after Push!");
    //     // for i in 0..NCPU {
    //     // slab.check_header(i, 49);
    //     // }
    //     let res2 = slab.pop(49).expect("Pop underflowed");
    //     let after = res2.as_ptr() as usize;
    //     assert_eq!(before, after);
    // }

    #[test]
    fn ccache_alloc_test() {
        let mut v = Vec::new();
        for i in 0..20 {
            let layout = Layout::from_size_align(8 * i, 8).expect("");
            let ptr = (*GLOBAL_CCACHE).allocate(layout).expect("");
            v.push((ptr, 8 * i));
        }

        for i in v {
            let layout = Layout::from_size_align(i.1, 8).expect("");
            (*GLOBAL_CCACHE).deallocate(i.0, layout);
        }
    }

    #[test]
    fn cacache_alloc_multithread_test() {
        let mut v = Vec::new();
        for _ in 0..1000 {
            let handle = thread::spawn(move || {
                let layout = Layout::from_size_align(8, 8).expect("");
                let res = (*GLOBAL_CCACHE).allocate(layout).expect("");
                let t = time::Duration::from_nanos(3);
                thread::sleep(t);
                (*GLOBAL_CCACHE).deallocate(res, layout);
            });
            v.push(handle);
        }

        for handle in v {
            handle.join().expect("cannot join");
        }
    }

    #[test]
    fn cacache_alloc_multithread_random_test() {
        let mut v = Vec::new();
        for _ in 0..1000 {
            let handle = thread::spawn(move || {
                let sz = rand::thread_rng().gen::<usize>() % 8192;
                let layout = Layout::from_size_align(sz, 8).expect("");
                let sec = rand::thread_rng().gen::<usize>() % 10;
                let res = (*GLOBAL_CCACHE).allocate(layout).expect("");
                let t = time::Duration::from_nanos(sec as u64);
                thread::sleep(t);
                (*GLOBAL_CCACHE).deallocate(res, layout);
            });
            v.push(handle);
        }

        for handle in v {
            handle.join().expect("cannot join");
        }
    }
}
