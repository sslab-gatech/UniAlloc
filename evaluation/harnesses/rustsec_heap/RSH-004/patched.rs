//! Pinned actix-codec 0.3.0-beta.1 control for RSH-004.
//!
//! The repaired API requires Pin<&mut Framed>; the control retains one pinned
//! allocation across both polls and exits cleanly under Miri.

use actix_codec::{AsyncRead, AsyncWrite, BytesCodec, Framed};
use futures::io::Error;
use futures::task::{noop_waker, Context, Poll};
use pin_project::pin_project;
use std::future::Future;
use std::pin::Pin;

#[pin_project]
struct FakeSocket<F> {
    #[pin]
    inner: F,
}

impl<F: Future> AsyncRead for FakeSocket<F> {
    fn poll_read(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
        _buffer: &mut [u8],
    ) -> Poll<Result<usize, Error>> {
        self.project().inner.poll(context).map(|_| Ok(0))
    }
}

impl<F> AsyncWrite for FakeSocket<F> {
    fn poll_write(
        self: Pin<&mut Self>,
        _context: &mut Context<'_>,
        _buffer: &[u8],
    ) -> Poll<Result<usize, Error>> {
        unimplemented!()
    }

    fn poll_flush(self: Pin<&mut Self>, _context: &mut Context<'_>) -> Poll<Result<(), Error>> {
        unimplemented!()
    }

    fn poll_shutdown(self: Pin<&mut Self>, _context: &mut Context<'_>) -> Poll<Result<(), Error>> {
        unimplemented!()
    }
}

fn main() {
    let (sender, receiver) = futures::channel::oneshot::channel();
    let mut framed = Box::pin(Framed::new(
        FakeSocket {
            inner: async {
                let allocation = Box::new(0);
                let reference = &allocation;
                receiver.await.expect("sender must complete");
                std::hint::black_box(**reference);
            },
        },
        BytesCodec,
    ));

    let waker = noop_waker();
    let mut context = Context::from_waker(&waker);
    let _ = framed.as_mut().next_item(&mut context);
    sender.send(()).expect("receiver must remain live");
    let _ = framed.as_mut().next_item(&mut context);
}
