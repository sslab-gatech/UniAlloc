#[path = "../src/boot_init_state.rs"]
mod boot_init_state;

use boot_init_state::BootInitState;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, Barrier};
use std::thread;
use std::time::{Duration, Instant};

const HEAP_A_START: usize = 0x1000_0000;
const HEAP_A_SIZE: usize = 0x0400_0000;
const HEAP_B_START: usize = 0x2000_0000;
const HEAP_B_SIZE: usize = 0x0200_0000;

#[test]
fn first_publish_invokes_initializer_once() {
    let state = BootInitState::new();
    let calls = AtomicUsize::new(0);

    assert!(state.publish(HEAP_A_START, HEAP_A_SIZE, || {
        calls.fetch_add(1, Ordering::Relaxed);
        true
    }));
    assert!(state.ready());
    assert_eq!(calls.load(Ordering::Relaxed), 1);
}

#[test]
fn same_range_is_idempotent_without_reinvoking_initializer() {
    let state = BootInitState::new();
    let calls = AtomicUsize::new(0);
    assert!(state.publish(HEAP_A_START, HEAP_A_SIZE, || {
        calls.fetch_add(1, Ordering::Relaxed);
        true
    }));

    assert!(state.publish(HEAP_A_START, HEAP_A_SIZE, || {
        calls.fetch_add(1, Ordering::Relaxed);
        false
    }));
    assert_eq!(calls.load(Ordering::Relaxed), 1);
}

#[test]
fn different_range_is_rejected_without_reinvoking_initializer() {
    let state = BootInitState::new();
    let calls = AtomicUsize::new(0);
    assert!(state.publish(HEAP_A_START, HEAP_A_SIZE, || {
        calls.fetch_add(1, Ordering::Relaxed);
        true
    }));

    assert!(!state.publish(HEAP_B_START, HEAP_B_SIZE, || {
        calls.fetch_add(1, Ordering::Relaxed);
        true
    }));
    assert_eq!(calls.load(Ordering::Relaxed), 1);
}

#[test]
fn failed_initialization_can_be_retried() {
    let state = BootInitState::new();
    let calls = AtomicUsize::new(0);

    assert!(!state.publish(HEAP_A_START, HEAP_A_SIZE, || {
        calls.fetch_add(1, Ordering::Relaxed);
        false
    }));
    assert!(!state.ready());
    assert!(state.publish(HEAP_B_START, HEAP_B_SIZE, || {
        calls.fetch_add(1, Ordering::Relaxed);
        true
    }));
    assert!(state.ready());
    assert_eq!(calls.load(Ordering::Relaxed), 2);
}

#[test]
fn concurrent_callers_wait_and_accept_only_the_winning_range() {
    let state = Arc::new(BootInitState::new());
    let release_winner = Arc::new(Barrier::new(2));
    let (winner_entered_tx, winner_entered_rx) = mpsc::channel();

    let winner_state = Arc::clone(&state);
    let winner_release = Arc::clone(&release_winner);
    let winner = thread::spawn(move || {
        winner_state.publish(HEAP_A_START, HEAP_A_SIZE, || {
            winner_entered_tx.send(()).unwrap();
            winner_release.wait();
            true
        })
    });
    winner_entered_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("winning initializer did not start");

    let (result_tx, result_rx) = mpsc::channel();
    let same_state = Arc::clone(&state);
    let same_tx = result_tx.clone();
    let same = thread::spawn(move || {
        let accepted = same_state.publish(HEAP_A_START, HEAP_A_SIZE, || {
            panic!("same-range waiter must not run its initializer")
        });
        same_tx.send(("same", accepted)).unwrap();
    });

    let different_state = Arc::clone(&state);
    let different = thread::spawn(move || {
        let accepted = different_state.publish(HEAP_B_START, HEAP_B_SIZE, || {
            panic!("different-range waiter must not run its initializer")
        });
        result_tx.send(("different", accepted)).unwrap();
    });

    let deadline = Instant::now() + Duration::from_secs(2);
    while state.wait_observations() < 2 && Instant::now() < deadline {
        thread::yield_now();
    }
    assert_eq!(
        state.wait_observations(),
        2,
        "both callers must observe the in-progress publication"
    );
    assert!(
        result_rx.try_recv().is_err(),
        "waiter returned before the winning initializer completed"
    );

    release_winner.wait();
    assert!(winner.join().unwrap());

    let first_result = result_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let second_result = result_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let mut results = [first_result, second_result];
    results.sort_unstable_by_key(|(label, _)| *label);
    assert_eq!(results, [("different", false), ("same", true)]);

    same.join().unwrap();
    different.join().unwrap();
}
