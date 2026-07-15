//! Verbatim-shape advisory witness for RUSTSEC-2022-0028 (`neon` < 0.10.1).
//!
//! This is intentionally retained as source evidence rather than converted to
//! a standalone synthetic binary: `FunctionContext` must be supplied by a live
//! Node/V8 addon invocation.

use neon::prelude::*;

pub fn soundness_hole(mut cx: FunctionContext) -> JsResult<JsArrayBuffer> {
    let mut data = vec![0u8, 1, 2, 3];
    let buf = JsArrayBuffer::external(&mut cx, data.as_mut_slice());
    drop(data);
    Ok(buf)
}
