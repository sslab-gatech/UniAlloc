use oneringbuf::LocalHeapRB;

fn main() {
    let rb = LocalHeapRB::<usize>::from(vec![1, 2, 3]);
    let (mut producer, mut consumer) = rb.split();

    producer.push(7).expect("ring buffer has capacity");
    assert_eq!(consumer.pop(), Some(7));

    drop(producer);
    drop(consumer);
}
