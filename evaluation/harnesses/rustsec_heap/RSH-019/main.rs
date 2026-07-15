//! RUSTSEC-2026-0005 / oneshot 0.1.11.
//!
//! This mechanical standalone adapter preserves upstream's
//! `tx_drop_rx_poll_then_drop` Miri regression test.  Miri's scheduler exposes
//! a race between the receiver dropping its stored waker and the sender
//! deallocating the channel.  The 0.1.12 patched release completes the same
//! multi-seed run without undefined behavior.

use std::{
    future::Future,
    pin::pin,
    task::{self, Poll, Waker},
};

fn main() {
    let (sender, receiver) = oneshot::channel::<i32>();

    let receiver_thread = std::thread::spawn(move || {
        let mut receiver = pin!(receiver);
        let mut context = task::Context::from_waker(Waker::noop());
        match receiver.as_mut().poll(&mut context) {
            Poll::Ready(Ok(value)) => panic!("unexpected Ok({value})"),
            Poll::Ready(Err(_)) | Poll::Pending => (),
        }
    });

    let sender_thread = std::thread::spawn(move || drop(sender));
    receiver_thread.join().expect("receiver must complete");
    sender_thread.join().expect("sender must complete");
}
