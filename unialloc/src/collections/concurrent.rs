use crate::pal::os::rseq::*;
use crate::prelude::*;
use crate::zone::{ZoneAllocator, GLOBAL_ZONE};
use crate::{
    error::{AllocError, Result},
    prelude::TOTAL_SIZE_CLASS,
};
use alloc::alloc::Layout;
use core::mem::{size_of, MaybeUninit};
use core::ptr::NonNull;
use core::sync::atomic::{AtomicUsize, Ordering};

#[repr(C)]
struct FlatArrayHeader {
    current: u16,
    end_copy: u16,
    begin: u16,
    end: u16,
}

impl core::convert::From<usize> for FlatArrayHeader {
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

impl core::convert::From<FlatArrayHeader> for usize {
    fn from(hdr: FlatArrayHeader) -> usize {
        let mut res: usize = 0;
        res += (hdr.end as usize) << 48;
        res += (hdr.begin as usize) << 32;
        res += (hdr.end_copy as usize) << 16;
        res += hdr.current as usize;
        res
    }
}

impl FlatArrayHeader {
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

/// A self-contained flatten array list
///
/// The size of the `FlatArrayList` is hardcorded with a const genric `SHIFT`
/// (the total size of this strcture is  1 << SHIFT),
/// and it has `NUM_ARRS` number of arrays
#[repr(C)]
pub struct FlatArrayList<const NUM_ARRS: usize, const DATA_LEN: usize>
where
    [(); NUM_ARRS]: Sized,
{
    /// hdr stores metadata, which is [`FlatArrayHeader`] structure.
    ///
    /// All of the offsets in the header are relative to the address of [`FlatArrayList`]
    ///
    /// FlatArrayList[i][begin] is always unavilable, because it always store the
    /// address of itself for valid prefetch operations.
    hdrs: [AtomicUsize; NUM_ARRS],

    /// data stores the elements of `FlatArrayList`
    data: [usize; DATA_LEN],
}

impl<const NUM_ARRS: usize, const DATA_LEN: usize> FlatArrayList<NUM_ARRS, DATA_LEN>
where
    [(); NUM_ARRS]: Sized,
{
    const HDR_SIZE: usize = size_of::<[AtomicUsize; NUM_ARRS]>();
    // const TOTAL_SIZE: usize = 1 << SHIFT;
    // const DATA_LEN: usize =
    // (Self::TOTAL_SIZE - size_of::<[AtomicUsize; NUM_ARRS]>()) / size_of::<usize>();

    pub fn new() -> Self {
        Self {
            hdrs: {
                let mut res: [AtomicUsize; NUM_ARRS] =
                    unsafe { core::mem::MaybeUninit::uninit().assume_init() };
                for item in res.iter_mut() {
                    *item = AtomicUsize::new(0usize);
                }
                res
            },
            data: [0usize; DATA_LEN],
        }
    }

    pub fn init(&self) {
        // Lock all of the headers
        for i in 0..NUM_ARRS {
            // TODO: discuss whether this operation is unsafe
            let mut hdr: FlatArrayHeader = self.hdrs[i].load(Ordering::Relaxed).into();
            hdr.lock();
            self.hdrs[i].store(hdr.into(), Ordering::Relaxed);
        }

        let start_addr = &self.hdrs as *const _ as usize;
        let mut byte_used = Self::HDR_SIZE;

        for cl in 0..NUM_ARRS {
            let current_addr = start_addr + byte_used;
            let cap = Self::initial_capacity();

            // set up prefetch target
            if cap != 0 {
                // Safety: The first element will not be used, and it is only
                // used as a prefetch target
                let target: *mut usize = current_addr as *const usize as *mut usize;
                unsafe {
                    *target = current_addr;
                }
            }

            let offset = byte_used / size_of::<*mut u8>();
            byte_used += (cap) * size_of::<*mut u8>();

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
            let hdr = FlatArrayHeader::new(current, end_copy, begin, end);
            // 3. allowing access the current cache
            self.hdrs[cl].store(hdr.into(), Ordering::Relaxed);
        }
    }

    /// The initial capacity for each size class
    ///
    /// For example, if there are 63 size classes, the data region's size is
    /// (1 << 18) - (8 * 63) = 261640
    ///
    /// The total number of slots is 261640 / 8 = 32705
    /// For 63 different size classes, a single class has (32705 / 63) = 519
    const fn initial_capacity() -> usize {
        (DATA_LEN >> 3) / TOTAL_SIZE_CLASS - 1
    }
}

// impl core::IndexMut<Idx> for FlatArrayList
// where
//     Idx: core::slice::SliceIndex<[usize]>
// {
//     type Output = usize;

//     fn index_mut(&mut self, index: Idx) -> &mut Self::Output {

//     }
// }

// impl core::Index<Idx> for FlatArrayList
// where
//     Idx: core::slice::SliceIndex<[usize]>
// {
//     type Output = usize;

//     fn index(&self, index: Idx) -> &Self::Output {

//     }
// }

const SHIFT: usize = 18;
const DATALEN: usize =
    ((1 << SHIFT) - size_of::<[AtomicUsize; TOTAL_SIZE_CLASS]>()) / size_of::<usize>();

/// A race-free per cpu array data structure
///
/// A single ArrayList in `arrs` is cpu-local. In this allocator, this
/// data structure is used for front-end cpu cache,
///
/// In Linux userspace, the percpu mechanism is backended by restarable sequences.
/// See https://www.efficios.com/blog/2019/02/08/linux-restartable-sequences/
///
/// In Linux kernelspace, it is implemented by disabling preemption
#[repr(C)]
pub struct PerCpuArray<const NCPU: usize> {
    arrs: [FlatArrayList<TOTAL_SIZE_CLASS, DATALEN>; NCPU],
}

/// APIs for allocation
impl<const NCPU: usize> PerCpuArray<NCPU> {
    pub fn new() -> Self {
        Self {
            // arrs: array_init! {FlatArrayList<TOTAL_SIZE_CLASS, DATALEN>, NCPU, FlatArrayList::<TOTAL_SIZE_CLASS, DATALEN>::new()},
            arrs: {
                let mut res: [FlatArrayList<TOTAL_SIZE_CLASS, DATALEN>; NCPU] =
                    unsafe { core::mem::MaybeUninit::uninit().assume_init() };

                for item in res.iter_mut() {
                    *item = FlatArrayList::<TOTAL_SIZE_CLASS, DATALEN>::new();
                }
                res
            },
        }
    }

    pub fn init(&self) {
        for i in 0..NCPU {
            self.arrs[i].init()
        }
    }

    /// Pushes a item into the PerCpuArray.
    ///
    /// This method accepts any type that implements [`Into<usize>`] trait,
    /// because each single slot in the array is a `usize`.
    ///
    /// if the array is full, the `err_handler(cpu_id)` will be called to handle it
    #[inline(never)]
    fn push<T, E>(&self, idx: usize, val: T, mut err_handler: E) -> Result<()>
    where
        T: core::convert::Into<usize> + Copy,
        E: FnMut(usize) -> Result<()>,
    {
        let r11: usize;
        let before: usize;
        let scratch: usize;
        let slab_addr = &self.arrs as *const _ as usize;

        register_current_thread_checked();

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
                slabs = in(reg) &self.arrs as *const _ as usize,
                cl = in(reg) idx, // size class index
                item = in(reg) val.into(), // pointer
                before = out(reg) before,
                out("r10") scratch, // clobbered
                out("r11") r11, // current (after)
            );
        }
        if r11 == before + 1 {
            debug_assert_ne!(val.into(), 0usize);
            Ok(())
        } else {
            // overflow and return cpu id
            let offset = scratch - slab_addr;
            let cpu_id = offset / (1 << SHIFT);

            err_handler(cpu_id)
        }
    }

    /// Pops an item from the array
    ///
    /// This method automatically converts an item in the specificed to
    /// `T` and returns it.
    ///
    /// If the array is empty, the `err_handler` will be called to handle it
    #[inline(never)]
    fn pop<T, E>(&self, idx: usize, mut err_handler: E) -> Result<T>
    where
        T: core::convert::From<usize>,
        E: FnMut(usize) -> Result<T>,
    {
        let scratch: usize;
        let before: usize;
        let current: usize; //after
        let result: usize;
        let slab_addr = &self.arrs as *const _ as usize;

        register_current_thread_checked();

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
                cl = in(reg) idx, // size class
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
            let cpu_id = offset / (1 << SHIFT);

            // call error handler
            // e.g., if there is no items in the array, we will refill the
            // array and allocate one from the backend allocator
            err_handler(cpu_id)
        } else {
            // println!("0x{:x}", result as usize);
            // println!("cur: {}", current);
            Ok(result.into())
        }
    }

    /// Pops an iterm from a specific array
    ///
    /// Druing poping, `pop_filter` checks whether the candidate meets the
    /// predicate described by `_filter` function
    fn pop_filter<T, F, E>(&self, _idx: usize, _filter: F, _err_handler: E) -> Result<usize>
    where
        T: core::convert::From<usize>,
        F: FnMut(usize) -> bool,
        E: FnMut(),
    {
        todo!()
    }
}

pub struct PerCpuCache {
    // TODO: modify the NCPU parameter to a dynamic variable
    cache: PerCpuArray<16>,
}

impl PerCpuCache {
    pub fn new() -> Self {
        Self {
            cache: PerCpuArray::new(),
        }
    }

    pub fn init(&self) {
        self.cache.init();
    }

    pub fn allocate(&mut self, layout: Layout) -> Result<NonNull<u8>> {
        let cls = get_size_class(layout.size());

        if let SizeClass::Base(idx) = cls {
            let ret = self
                .cache
                .pop(idx, |cpu| {
                    // refill
                    let mut batch = [0; 512];

                    const CAP: usize = 128;

                    if cpu_id() as usize == cpu {
                        let res = (*GLOBAL_ZONE).allocate_batch_from_slab(idx, &mut batch, CAP);
                        if let Ok(ptr) = res {
                            let mut curr = 1;
                            while curr < CAP {
                                let item = batch[curr - 1];
                                let result = self.cache.push(item, idx, |_| Err(AllocError::ECPU));

                                if result.is_err() {
                                    break;
                                };

                                curr += 1;
                            }

                            if curr < CAP {
                                (*GLOBAL_ZONE)
                                    .deallocate_batch_to_slab(idx, &mut batch[curr..], 128 - curr)
                                    .expect("return unused chunks error!");
                            }

                            return Ok(ptr.as_ptr() as usize);
                        }
                    }

                    Ok((*GLOBAL_ZONE)
                        .allocate_from_slab(idx, layout.align())
                        .unwrap()
                        .as_ptr() as usize)
                })
                .unwrap();

            return Ok(NonNull::new(ret as *mut u8).unwrap());
        } else if let SizeClass::Large(_) = cls {
            return (*GLOBAL_ZONE).allocate(layout.size(), layout.align());
        }

        Err(AllocError::ESIZE)
    }

    pub fn deallocate(&mut self, ptr: NonNull<u8>, layout: Layout) {
        let cls = get_size_class(layout.size());

        if let SizeClass::Base(idx) = cls {
            self.cache
                .push(idx, ptr.as_ptr() as usize, |cpu| {
                    let mut batch = [0usize; 512];

                    // TODO: check whether this coversion is safe
                    if cpu_id() as usize == cpu {
                        batch[0] = ptr.as_ptr() as usize;

                        const CAP: usize = 128;

                        let mut curr = 1;
                        while curr < CAP {
                            if let Ok(p) = self
                                .cache
                                .pop::<usize, _>(idx, |_cpu| Err(AllocError::ECPU))
                            {
                                batch[curr] = p;
                                curr += 1;
                            } else {
                                break;
                            }
                        }

                        if curr < CAP {
                            (*GLOBAL_ZONE)
                                .deallocate_batch_to_slab(idx, &mut batch, curr)
                                .expect("return unused chunks error!");
                        }
                    }

                    Ok(())
                })
                .expect("push failed");
        } else if let SizeClass::Large(req_sz) = cls {
            (*GLOBAL_ZONE).deallocate(ptr, req_sz)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
}
