use metacall::MetaCallPointer;

fn main() {
    // Safe-API UB: clone() shares the same rust_value pointer.
    // Calling get_value_untyped on both clones will Box::from_raw the same pointer twice.
    let p1 = MetaCallPointer::new(String::from("hi"));
    let p2 = p1.clone();

    let v1 = p2.get_value_untyped();
    let v2 = p1.get_value_untyped();

    drop(v1);
    drop(v2);
}
