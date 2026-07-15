//! RUSTSEC-2026-0103 / thin-vec 0.2.15.
//!
//! This mechanical standalone adapter preserves the advisory's
//! `IntoIter::drop` trigger. Run in a child process: panic during iterator
//! cleanup can make unwinding drop already-dropped elements again, producing
//! an ASan/Miri double-free report or process abort.

use thin_vec::ThinVec;

struct PanicBomb {
    payload: String,
    panic_on_drop: bool,
}

impl PanicBomb {
    fn new(payload: &str, panic_on_drop: bool) -> Self {
        Self {
            payload: payload.into(),
            panic_on_drop,
        }
    }
}

impl Drop for PanicBomb {
    fn drop(&mut self) {
        if self.panic_on_drop {
            panic!("intentional drop panic");
        }
        std::hint::black_box(&self.payload);
    }
}

fn main() {
    let mut values = ThinVec::new();
    values.push(PanicBomb::new("normal1", false));
    values.push(PanicBomb::new("panic", true));
    values.push(PanicBomb::new("normal2", false));

    let mut iterator = values.into_iter();
    drop(iterator.next());
    drop(iterator);
}
