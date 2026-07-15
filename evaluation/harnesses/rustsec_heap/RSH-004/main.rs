//! RUSTSEC-2020-0049 / actix-codec 0.2.0.
//!
//! The upstream-issue adapter moves a `Framed` after its safe API has treated
//! the inner future as pinned. Miri supplies the provenance/retag oracle.

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
    let mut framed: Result<_, [u8; 32]> = Ok(Framed::new(
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
    let _ = framed
        .as_mut()
        .expect("framed value")
        .next_item(&mut context);
    sender.send(()).expect("receiver must remain live");
    let _ = std::mem::replace(&mut framed, Err([0; 32]))
        .expect("framed value")
        .next_item(&mut context);
}
