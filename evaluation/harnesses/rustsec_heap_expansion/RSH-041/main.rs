use oneringbuf::{IntoRef, LocalHeapRB};

fn main() {
    let rb = LocalHeapRB::<usize>::from(vec![1, 2, 3]);

    let r = <LocalHeapRB<usize> as IntoRef>::into_ref(rb);
    let r2 = r.clone();
    let r3 = r.clone();

    drop(r);
    drop(r2);
    drop(r3); // AddressSanitizer: heap-use-after-free
}
