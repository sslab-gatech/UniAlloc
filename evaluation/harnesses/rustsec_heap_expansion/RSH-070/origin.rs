#[test]
fn gh_971() {
    let (s1, r) = unbounded::<u64>();
    let s2 = s1.clone();

    // This thread will sleep for 2000ms at the critical moment
    let t1 = thread::spawn(move || assert!(s1.send(42).is_err()));

    // Give some time for thread 1 to reach the critical state
    thread::sleep(Duration::from_millis(100));
    // Send another value which see the tail is not null and will just advance the tail offset to 1
    // and write the value.
    s2.send(42).unwrap();

    // Now drop the receiver which will attempt to drop a message by reading it since head != tail
    // but head is still a null pointer because the thread t1 is still preempted leading to a
    // segfault.
    drop(r);

    t1.join().unwrap();
}
