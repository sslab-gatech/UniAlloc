use super::*;
use core::fmt::{self, Write};

#[cfg(target_os = "linux")]
fn sys_write(fd: usize, buf: *const u8, len: usize) -> isize {
    syscall3(1, [fd as usize, buf as usize, len as usize])
}

#[cfg(target_os = "windows")]
fn sys_write(fd: usize, buf: *const u8, len: usize) -> isize {
    syscall3(1, [fd as usize, buf as usize, len as usize])
}

#[cfg(target_os = "macos")]
fn sys_write(fd: usize, buf: *const u8, len: usize) -> isize {
    // https://github.com/apple/darwin-xnu/blob/main/bsd/kern/syscalls.master
    syscall3(0x2000004, [fd as usize, buf as usize, len as usize])
}

fn put_char(c: usize) {
    #[cfg(not(feature = "fixed_heap"))]
    sys_write(2, &c as *const _ as *const u8, 1);
}

struct Stdout;

impl Write for Stdout {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        for c in s.chars() {
            put_char(c as usize)
        }
        Ok(())
    }
}

pub fn print(args: fmt::Arguments) {
    Stdout.write_fmt(args).expect("cannot print");
}

#[macro_export]
macro_rules! print {
    ($fmt: literal $(, $($arg: tt)+)?) => {
        $crate::pal::arch::print::print(format_args!($fmt $(, $($arg)+)?));
    }
}

#[macro_export]
macro_rules! println {
    ($fmt: literal $(, $($arg: tt)+)?) => {
        $crate::pal::arch::print::print(format_args!(concat!($fmt, "\n") $(, $($arg)+)?));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn print_test() {
        println!("Hello World 1337!");
    }
}
