include!(concat!(env!("OUT_DIR"), "/consts.rs"));

fn main() {
    println!("has rseq: {}", HAS_RSEQ);
}
