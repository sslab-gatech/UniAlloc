//! Fail-closed local control for RSH-031. The patched library rejects a Vec
//! whose Drop implementation cannot preserve the requested over-alignment.

use maligned::{align_first, A256};

fn main() {
    let rejected = std::panic::catch_unwind(|| {
        let _: Vec<u8> = align_first::<u8, A256>(1009);
    });
    assert!(rejected.is_err(), "over-aligned Vec must be rejected");
}
