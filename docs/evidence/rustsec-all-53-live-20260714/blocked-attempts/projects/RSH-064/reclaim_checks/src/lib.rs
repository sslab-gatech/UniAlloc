use neon::prelude::*;
use unialloc::UniAlloc;

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

pub fn soundness_hole(mut cx: FunctionContext) -> JsResult<JsArrayBuffer> {
    let mut data = vec![0u8, 1, 2, 3];
    let buf = JsArrayBuffer::external(&mut cx, data.as_mut_slice());
    drop(data);
    Ok(buf)
}

#[neon::main]
fn main(mut cx: ModuleContext) -> NeonResult<()> {
    cx.export_function("soundnessHole", soundness_hole)?;
    Ok(())
}
