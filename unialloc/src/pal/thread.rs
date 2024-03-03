pub mod linux {
    extern crate std;
    pub use std::thread;
}

#[cfg(test)]
mod test {
    // extern crate std;
    // use std::thread;
    use super::linux::thread;

    #[test]
    fn test_std_thread() {
        let computation = thread::spawn(|| {
            // Some expensive computation.
            42
        });

        let result = computation.join().unwrap();
    }
}
