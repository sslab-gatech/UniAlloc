use super::*;

#[cfg(target_os = "linux")]
pub mod linux_syscalls {
    use super::*;

    fn sys_write(fd: usize, buf: *const u8, len: usize) -> isize {
        syscall3(1, [fd as usize, buf as usize, len as usize])
    }
}

#[cfg(target_os = "windows")]
pub mod windows_syscalls {
    use super::*;

    fn sys_write(fd: usize, buf: *const u8, len: usize) -> isize {
        syscall3(1, [fd as usize, buf as usize, len as usize])
    }
}

#[cfg(target_os = "macos")]
pub mod macos_syscalls {
    use super::*;

    fn sys_write(fd: usize, buf: *const u8, len: usize) -> isize {
        // https://github.com/apple/darwin-xnu/blob/main/bsd/kern/syscalls.master
        syscall3(0x2000004, [fd as usize, buf as usize, len as usize])
    }
}

#[cfg(target_os = "linux")]
pub use linux_syscalls as syscall;

#[cfg(target_os = "windows")]
pub use windows_syscalls as syscall;

#[cfg(target_os = "macos")]
pub use macos_syscalls as syscall;
