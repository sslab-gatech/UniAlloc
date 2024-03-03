#[allow(deprecated)]
use core::arch::asm;

// #[cfg(target_arch = "x86_64")]
// pub fn llvm_syscall4(nr: usize, args: [usize; 4]) -> isize {
//     let mut ret: isize;
//     unsafe {
//         llvm_asm!("syscall"
//         :"={rax}"(ret)
//         :"{rax}"(nr), "{rdi}"(args[0]), "{rsi}"(args[1]), "{rdx}"(args[2]), "{r10}" (args[3])
//         : "memory"
//         : "volatile"
//         );
//     }
//     ret
// }

#[cfg(target_arch = "x86_64")]
pub fn syscall4(nr: usize, args: [usize; 4]) -> isize {
    let ret: isize;
    unsafe {
        // new asm feature supports x86/x86_64, ARM, AArch64 and RISC-V
        // https://rust-lang.github.io/rfcs/2873-inline-asm.html
        asm!(
            "syscall",
            in("rax") nr, // syscall number
            in("rdi") args[0],
            in("rsi") args[1],
            in("rdx") args[2],
            in("r10") args[3],
            out("rcx") _, // clobbered by syscalls
            out("r11") _, // clobbered by syscalls
            lateout("rax") ret,
        );
    }
    ret
}

#[cfg(target_arch = "x86_64")]
pub fn syscall3(nr: usize, args: [usize; 3]) -> isize {
    let ret: isize;
    unsafe {
        asm!(
            "syscall",
            in("rax") nr, // syscall number
            in("rdi") args[0],
            in("rsi") args[1],
            in("rdx") args[2],
            out("rcx") _, // clobbered by syscalls
            out("r11") _, // clobbered by syscalls
            lateout("rax") ret,
        );
    }
    ret
}
