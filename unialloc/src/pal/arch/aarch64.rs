#[cfg(target_arch = "aarch64")]
pub fn syscall4(nr: usize, args: [usize; 4]) -> isize {
    let ret: isize;
    unsafe {
        asm!(
            "svc #0",
            in("x8") nr, // syscall number
            in("x0") args[0],
            in("x1") args[1],
            in("x2") args[2],
            in("x3") args[3],
            out("x4") _, // clobbered by syscalls
            out("x5") _, // clobbered by syscalls
            lateout("x0") ret,
        );
    }
    ret
}

#[cfg(target_arch = "aarch64")]
pub fn syscall3(nr: usize, args: [usize; 3]) -> isize {
    // x16: NR
    // x0 ~ x8 : 9 arguments
    // x0 and x1 hold two return values (e.g., fork)
    let ret: isize;
    unsafe {
        asm!(
            "svc 0x80",
            in("x16") nr, // syscall number
            in("x0") args[0],
            in("x1") args[1],
            in("x2") args[2],
            // out("x3") _, // clobbered by syscalls
            // out("x4") _, // clobbered by syscalls
            // out("x5") _, // clobbered by syscalls
            // out("x6") _, // clobbered by syscalls
            lateout("x0") ret,
        );
    }
    ret
}
