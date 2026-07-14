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
    use std::thread::spawn;
    use std::vec::Vec;

    const PRIVATE_RSEQ_TEST_CHILD: &str = "UNIALLOC_PRIVATE_RSEQ_TEST_CHILD";

    fn run_private_rseq_test_in_fresh_process(test_name: &str) -> bool {
        if std::env::var(PRIVATE_RSEQ_TEST_CHILD).ok().as_deref() == Some(test_name) {
            return false;
        }

        // Modern glibc registers its own rseq area before a Rust test thread
        // starts. Linux permits only one registered area per thread, so test
        // UniAlloc's private registration in a child whose loader has disabled
        // libc-managed rseq. The parent and normal applications keep glibc's
        // registration unchanged.
        let output = std::process::Command::new(
            std::env::current_exe().expect("current allocator test executable"),
        )
        .args(["--exact", test_name, "--nocapture", "--test-threads=1"])
        .env(PRIVATE_RSEQ_TEST_CHILD, test_name)
        .env("GLIBC_TUNABLES", "glibc.pthread.rseq=0")
        .output()
        .expect("spawn isolated private-rseq test");

        assert!(
            output.status.success(),
            "isolated private-rseq test failed\nstdout:\n{}\nstderr:\n{}",
            std::string::String::from_utf8_lossy(&output.stdout),
            std::string::String::from_utf8_lossy(&output.stderr),
        );
        true
    }

    fn sys_write(fd: usize, buf: *const u8, len: usize) -> isize {
        syscall3(1, [fd as usize, buf as usize, len as usize])
    }

    #[test]
    fn syscall_test() {
        let buf = "Hello from asm!\n";
        assert_eq!(sys_write(1, buf.as_ptr(), buf.len()) as usize, buf.len());
    }

    #[test]
    fn rseq_layout_matches_kernel_abi() {
        assert_eq!(mem::size_of::<rseq>(), 32);
        assert_eq!(mem::align_of::<rseq>(), 32);
        assert_eq!(mem::size_of::<rseq_cs>(), 32);
        assert_eq!(mem::align_of::<rseq_cs>(), 32);
    }

    #[test]
    fn register_rseq_test() {
        if run_private_rseq_test_in_fresh_process("pal::os::linux_rseq::tests::register_rseq_test")
        {
            return;
        }

        register_current_thread();
        assert!(is_registered());
        assert!(cpu_id_start() >= 0);

        // The scheduler may migrate this thread between reads. Registration
        // guarantees a valid CPU id, not a constant CPU id.
        for _ in 0..1000 {
            assert!(cpu_id() >= 0);
        }

        unregister_current_thread();
        assert!(!is_registered());
    }

    #[test]
    fn register_rseq_multithread_test() {
        if run_private_rseq_test_in_fresh_process(
            "pal::os::linux_rseq::tests::register_rseq_multithread_test",
        ) {
            return;
        }

        let handles: Vec<_> = (0..100)
            .map(|_| {
                spawn(move || {
                    register_current_thread();
                    assert!(is_registered());
                    unregister_current_thread();
                    assert!(!is_registered());
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
            assert_eq!(-1, rc);
            assert_eq!(22, *libc::__errno_location());
        }
    }
}
