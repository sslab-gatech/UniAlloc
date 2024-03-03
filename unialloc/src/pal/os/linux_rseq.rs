//! Utils for RSeq
//!
//! References:
//!
//! https://github.com/torvalds/linux/blob/master/include/uapi/linux/rseq.h
use crate::*;
use core::arch::asm;
use core::mem;
use core::ptr::read_volatile;
use libc::perror;

// nr_rseq
// x64 334
// aarch64 293
// ppc 387

// rseq_cpu_id_state
const RSEQ_CPU_ID_UNINITIALIZED: isize = -1;
const RSEQ_CPU_ID_REGISTRATION_FAILED: isize = -1;

// rseq_flags
const RSEQ_FLAG_UNREGISTER: usize = 1 << 0;

// rseq_cs_flags_bit
const RSEQ_CS_FLAG_NO_RESTART_ON_PREEMPT_BIT: usize = 0;
const RSEQ_CS_FLAG_NO_RESTART_ON_SIGNAL_BIT: usize = 1;
const RSEQ_CS_FLAG_NO_RESTART_ON_MIGRATE_BIT: usize = 2;

// const kRseqUnregister: isize = 1;

// Internal state used for tracking initialization of RseqCpuId()
// const kCpuIdUnsupported: isize = -2;
// const kCpuIdUninitialized: isize = -1;
// const kCpuIdInitialized: isize = 0;

#[repr(C, align(32))]
pub struct rseq_cs {
    version: u32,
    flags: u32,
    start_ip: u64,
    post_commit_offset: u64,
    abort_ip: u64,
}

#[repr(C, align(32))]
pub struct rseq {
    cpu_id_start: i32,
    cpu_id: i32,
    ptr64: *const rseq_cs, // a pointer to rseq_cs
    flags: u32,
}

impl rseq {
    pub const fn new() -> Self {
        Self {
            // cpu_id_start is guaranteed to be a possible CPU id
            cpu_id_start: 0,

            // we use "-1" to whether whether the current thread is registered
            cpu_id: -1,
            ptr64: 0 as *const rseq_cs,
            flags: 0,
        }
    }
}

#[repr(C, align(32))]
pub struct rseq2 {
    cpu_id_start: i32,
    cpu_id: i32,
    ptr64: *const rseq_cs, // a pointer to rseq_cs
    flags: u32,
    padding: [u32; 2],
    // copied from tcmalloc
    // This is a prototype extension to the rseq() syscall.  Since a process may
    // run on only a few cores at a time, we can use a dense set of "v(irtual)
    // cpus."  This can reduce cache requirements, as we only need N caches for
    // the cores we actually run on simultaneously, rather than a cache for every
    // physical core.
    //   union {
    //     struct {
    //       short numa_node_id;
    //       short vcpu_id;
    //     };
    //     int vcpu_flat;
    //   };
    numa_nod_id: i16,
    vcpu_id: i16,
}

impl rseq2 {
    pub const fn new() -> Self {
        Self {
            // cpu_id_start is guaranteed to be a possible CPU id
            cpu_id_start: 0,

            // we use "-1" to whether whether the current thread is registered
            cpu_id: -1,
            ptr64: 0 as *const rseq_cs,
            flags: 0,
            padding: [0, 0],
            numa_nod_id: 0,
            vcpu_id: 0,
        }
    }
}

/// A wrapper for rseq - Restartable sequences and cpu number cache
fn sys_rseq(rseq: *const rseq, rseq_len: u32, flags: i32, sig: u32) -> isize {
    syscall4(
        334, // syscall number
        [
            rseq as usize,     // rseq pointer
            rseq_len as usize, // rseq_len
            flags as usize,    // flags
            sig as usize,      // signature
        ],
    )
}

#[thread_local]
pub static RSEQ_ABI: rseq = rseq::new();
pub const RSEQ_SIG: u32 = 0x53053053;

pub fn register_current_thread() {
    let rseq_abi: *const rseq = &RSEQ_ABI as *const rseq;
    let rc = sys_rseq(rseq_abi, mem::size_of::<rseq>() as u32, 0, RSEQ_SIG);
    // let rc = unsafe {libc::syscall(334, rseq_abi, mem::size_of_val(&RSEQ_ABI) as u32, 0, RSEQ_SIG)};

    if rc != 0 {
        // println!("[-] rseq register failed");
        panic!("Failed to register rseq");
        // panic!("Error Code: {}", unsafe { *libc::__errno_location() });
    }
}

pub fn unregister_current_thread() {
    let rseq_abi: *const rseq = &RSEQ_ABI as *const rseq;
    let rc = sys_rseq(rseq_abi, mem::size_of::<rseq>() as u32, 1, RSEQ_SIG);

    if rc != 0 {
        panic!("Failed to unregister rseq");
    }
}

#[inline]
pub fn cpu_id() -> i32 {
    unsafe { read_volatile(&RSEQ_ABI.cpu_id) }
}

#[inline]
pub fn cpu_id_start() -> i32 {
    unsafe { read_volatile(&RSEQ_ABI.cpu_id_start) }
}

#[inline]
pub fn is_registered() -> bool {
    cpu_id() >= 0
}

pub fn register_current_thread_checked() {
    if core::intrinsics::likely(is_registered()) {
        return;
    };
    register_current_thread();
}

pub fn unregister_current_thread_checked() {
    if !is_registered() {
        return;
    };
    unregister_current_thread();
}

#[cfg(target_os = "linux")]
#[cfg(test)]
mod tests {
    use super::*;
    use crate::*;
    extern crate std;
    use spin::Mutex;
    use std::boxed::Box;
    use std::collections::BTreeSet;
    use std::thread::spawn;
    use std::vec::Vec;

    fn sys_write(fd: usize, buf: *const u8, len: usize) -> isize {
        syscall3(1, [fd as usize, buf as usize, len as usize])
    }

    #[test]
    fn syscall_test() {
        let buf = "Hello from asm!\n";
        assert_eq!(sys_write(1, buf.as_ptr(), buf.len()) as usize, buf.len());
    }

    #[test]
    fn align_test() {
        assert_eq!(4 * mem::size_of::<u64>(), 32);
    }

    #[test]
    fn register_rseq_test() {
        register_current_thread();
        assert_eq!(is_registered(), true);
        assert!(RSEQ_ABI.cpu_id as usize <= NCPU);

        let cpuid = cpu_id();
        for _ in 0..1000 {
            assert_eq!(cpuid, cpu_id());
        }
    }

    #[test]
    fn register_rseq_multithread_test() {
        // let seen: &'static _ = Box::leak(Box::new(Mutex::new(BTreeSet::new())));
        let handles: Vec<_> = (0..100)
            .map(|_| {
                spawn(move || {
                    // let addr = &RSEQ_ABI as *const _ as usize;
                    // println!("{:x}", addr);
                    // if seen.lock().contains(&addr) {
                    // assert!(false, "The address cannot have duplications");
                    // }
                    // seen.lock().insert(addr);
                    register_current_thread();
                    assert_eq!(is_registered(), true);
                })
            })
            .collect();

        for handle in handles {
            handle.join().expect("");
        }
    }

    #[test]
    fn inline_asm_add_test() {
        let i: u64 = 3;
        let o: u64;
        unsafe {
            asm!(
                "mov {0}, {1}",
                "add {0}, {number}",
                out(reg) o,
                in(reg) i,
                number = const 5,
            );
        }
        assert_eq!(o, 8);
    }

    #[test]
    fn rseq_available() {
        unsafe {
            let rc = libc::syscall(334, 0, 0, 0, 0);
            // EINVAL: invalid argument
            assert_eq!(22, *libc::__errno_location());
        }
    }
}
