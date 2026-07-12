#![no_std]
#![feature(allocator_api, global_asm)]
#![feature(asm)]
#![feature(slice_partition_dedup)]

#[macro_use]
extern crate alloc;
// The bridge below reaches UniAlloc through `extern "C"` declarations, so the
// final module crate must still explicitly consume the rlib passed via
// `RUSTFLAGS_MODULE=--extern unialloc=...`.  Without this anonymous import,
// rustc is free to leave that otherwise-unused `--extern` crate out of the
// link, leaving the bridge ABI symbols unresolved.
extern crate unialloc as _;

mod unialloc_bridge;

use alloc::boxed::Box;
use alloc::vec::Vec;
use core::iter::{repeat, FromIterator};
use kernel::bencher::{bench_it, black_box};
use kernel::prelude::*;

pub struct Bencher {
    res: u64,
    pub bytes: u64,
    benchmark: &'static str,
}

impl Bencher {
    pub fn set_benchmark(&mut self, benchmark: &'static str) {
        self.benchmark = benchmark;
        self.bytes = 0;
    }

    pub fn iter<T, F>(&mut self, mut inner: F)
    where
        F: FnMut() -> T,
    {
        let res: &mut [u64; 450] = &mut [0u64; 450];

        for p in &mut *res {
            *p = bench_it(&mut inner);
        }

        res.sort();
        let mid = res.len() / 2;
        self.res = res[mid];
        pr_alert!("cycle: {}\n", self.res);
        pr_info!(
            "UNIALLOC_RFL_CYCLE_SAMPLE benchmark={} median_cycles={} bytes={}\n",
            self.benchmark,
            self.res,
            self.bytes
        );
    }
}

module! {
    type: RustMinimal,
    name: b"rust_minimal",
    author: b"Rust for Linux Contributors",
    description: b"Rust minimal sample",
    license: b"GPL v2",
    params: {
    },
}

fn bench_new(b: &mut Bencher) {
    b.iter(|| Vec::<u32>::new())
}

fn do_bench_with_capacity(b: &mut Bencher, src_len: usize) {
    b.bytes = src_len as u64;

    b.iter(|| Vec::<u32>::with_capacity(src_len))
}

fn bench_with_capacity_0000(b: &mut Bencher) {
    do_bench_with_capacity(b, 0)
}

fn bench_with_capacity_0010(b: &mut Bencher) {
    do_bench_with_capacity(b, 10)
}

fn bench_with_capacity_0100(b: &mut Bencher) {
    do_bench_with_capacity(b, 100)
}

fn bench_with_capacity_1000(b: &mut Bencher) {
    do_bench_with_capacity(b, 1000)
}

fn do_bench_from_fn(b: &mut Bencher, src_len: usize) {
    b.bytes = src_len as u64;

    b.iter(|| (0..src_len).collect::<Vec<_>>())
}

fn bench_from_fn_0000(b: &mut Bencher) {
    do_bench_from_fn(b, 0)
}

fn bench_from_fn_0010(b: &mut Bencher) {
    do_bench_from_fn(b, 10)
}

fn bench_from_fn_0100(b: &mut Bencher) {
    do_bench_from_fn(b, 100)
}

fn bench_from_fn_1000(b: &mut Bencher) {
    do_bench_from_fn(b, 1000)
}

fn do_bench_from_elem(b: &mut Bencher, src_len: usize) {
    b.bytes = src_len as u64;

    b.iter(|| repeat(5).take(src_len).collect::<Vec<usize>>())
}

fn bench_from_elem_0000(b: &mut Bencher) {
    do_bench_from_elem(b, 0)
}

fn bench_from_elem_0010(b: &mut Bencher) {
    do_bench_from_elem(b, 10)
}

fn bench_from_elem_0100(b: &mut Bencher) {
    do_bench_from_elem(b, 100)
}

fn bench_from_elem_1000(b: &mut Bencher) {
    do_bench_from_elem(b, 1000)
}

fn do_bench_from_slice(b: &mut Bencher, src_len: usize) {
    let src: Vec<_> = FromIterator::from_iter(0..src_len);

    b.bytes = src_len as u64;

    b.iter(|| src.as_slice().to_vec());
}

fn bench_from_slice_0000(b: &mut Bencher) {
    do_bench_from_slice(b, 0)
}

fn bench_from_slice_0010(b: &mut Bencher) {
    do_bench_from_slice(b, 10)
}

fn bench_from_slice_0100(b: &mut Bencher) {
    do_bench_from_slice(b, 100)
}

fn bench_from_slice_1000(b: &mut Bencher) {
    do_bench_from_slice(b, 1000)
}

fn do_bench_from_iter(b: &mut Bencher, src_len: usize) {
    let src: Vec<_> = FromIterator::from_iter(0..src_len);

    b.bytes = src_len as u64;

    b.iter(|| {
        let dst: Vec<_> = FromIterator::from_iter(src.iter().cloned());
        dst
    });
}

fn bench_from_iter_0000(b: &mut Bencher) {
    do_bench_from_iter(b, 0)
}

fn bench_from_iter_0010(b: &mut Bencher) {
    do_bench_from_iter(b, 10)
}

fn bench_from_iter_0100(b: &mut Bencher) {
    do_bench_from_iter(b, 100)
}

fn bench_from_iter_1000(b: &mut Bencher) {
    do_bench_from_iter(b, 1000)
}

fn do_bench_extend(b: &mut Bencher, dst_len: usize, src_len: usize) {
    let dst: Vec<_> = FromIterator::from_iter(0..dst_len);
    let src: Vec<_> = FromIterator::from_iter(dst_len..dst_len + src_len);

    b.bytes = src_len as u64;

    b.iter(|| {
        let mut dst = dst.clone();
        dst.extend(src.clone());
        dst
    });
}

fn bench_extend_0000_0000(b: &mut Bencher) {
    do_bench_extend(b, 0, 0)
}

fn bench_extend_0000_0010(b: &mut Bencher) {
    do_bench_extend(b, 0, 10)
}

fn bench_extend_0000_0100(b: &mut Bencher) {
    do_bench_extend(b, 0, 100)
}

fn bench_extend_0000_1000(b: &mut Bencher) {
    do_bench_extend(b, 0, 1000)
}

fn bench_extend_0010_0010(b: &mut Bencher) {
    do_bench_extend(b, 10, 10)
}

fn bench_extend_0100_0100(b: &mut Bencher) {
    do_bench_extend(b, 100, 100)
}

fn bench_extend_1000_1000(b: &mut Bencher) {
    do_bench_extend(b, 1000, 1000)
}

fn do_bench_extend_from_slice(b: &mut Bencher, dst_len: usize, src_len: usize) {
    let dst: Vec<_> = FromIterator::from_iter(0..dst_len);
    let src: Vec<_> = FromIterator::from_iter(dst_len..dst_len + src_len);

    b.bytes = src_len as u64;

    b.iter(|| {
        let mut dst = dst.clone();
        dst.extend_from_slice(&src);
        dst
    });
}

fn bench_extend_recycle(b: &mut Bencher) {
    let mut data = vec![0; 1000];

    b.iter(|| {
        let tmp = core::mem::take(&mut data);
        let mut to_extend = black_box(Vec::new());
        to_extend.extend(tmp.into_iter());
        data = black_box(to_extend);
    });

    black_box(data);
}

fn bench_extend_from_slice_0000_0000(b: &mut Bencher) {
    do_bench_extend_from_slice(b, 0, 0)
}

fn bench_extend_from_slice_0000_0010(b: &mut Bencher) {
    do_bench_extend_from_slice(b, 0, 10)
}

fn bench_extend_from_slice_0000_0100(b: &mut Bencher) {
    do_bench_extend_from_slice(b, 0, 100)
}

fn bench_extend_from_slice_0000_1000(b: &mut Bencher) {
    do_bench_extend_from_slice(b, 0, 1000)
}

fn bench_extend_from_slice_0010_0010(b: &mut Bencher) {
    do_bench_extend_from_slice(b, 10, 10)
}

fn bench_extend_from_slice_0100_0100(b: &mut Bencher) {
    do_bench_extend_from_slice(b, 100, 100)
}

fn bench_extend_from_slice_1000_1000(b: &mut Bencher) {
    do_bench_extend_from_slice(b, 1000, 1000)
}

fn do_bench_clone(b: &mut Bencher, src_len: usize) {
    let src: Vec<usize> = FromIterator::from_iter(0..src_len);

    b.bytes = src_len as u64;

    b.iter(|| src.clone());
}

fn bench_clone_0000(b: &mut Bencher) {
    do_bench_clone(b, 0)
}

fn bench_clone_0010(b: &mut Bencher) {
    do_bench_clone(b, 10)
}

fn bench_clone_0100(b: &mut Bencher) {
    do_bench_clone(b, 100)
}

fn bench_clone_1000(b: &mut Bencher) {
    do_bench_clone(b, 1000)
}

fn do_bench_clone_from(b: &mut Bencher, times: usize, dst_len: usize, src_len: usize) {
    let dst: Vec<_> = FromIterator::from_iter(0..dst_len);
    let src: Vec<_> = FromIterator::from_iter(dst_len..dst_len + src_len);

    b.bytes = (times * src_len) as u64;

    b.iter(|| {
        let mut dst = dst.clone();

        for _ in 0..times {
            dst.clone_from(&src);
            dst = black_box(dst);
        }
        dst
    });
}

fn bench_clone_from_01_0000_0000(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 0, 0)
}

fn bench_clone_from_01_0000_0010(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 0, 10)
}

fn bench_clone_from_01_0000_0100(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 0, 100)
}

fn bench_clone_from_01_0000_1000(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 0, 1000)
}

fn bench_clone_from_01_0010_0010(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 10, 10)
}

fn bench_clone_from_01_0100_0100(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 100, 100)
}

fn bench_clone_from_01_1000_1000(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 1000, 1000)
}

fn bench_clone_from_01_0010_0100(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 10, 100)
}

fn bench_clone_from_01_0100_1000(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 100, 1000)
}

fn bench_clone_from_01_0010_0000(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 10, 0)
}

fn bench_clone_from_01_0100_0010(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 100, 10)
}

fn bench_clone_from_01_1000_0100(b: &mut Bencher) {
    do_bench_clone_from(b, 1, 1000, 100)
}

fn bench_clone_from_10_0000_0000(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 0, 0)
}

fn bench_clone_from_10_0000_0010(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 0, 10)
}

fn bench_clone_from_10_0000_0100(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 0, 100)
}

fn bench_clone_from_10_0000_1000(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 0, 1000)
}

fn bench_clone_from_10_0010_0010(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 10, 10)
}

fn bench_clone_from_10_0100_0100(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 100, 100)
}

fn bench_clone_from_10_1000_1000(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 1000, 1000)
}

fn bench_clone_from_10_0010_0100(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 10, 100)
}

fn bench_clone_from_10_0100_1000(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 100, 1000)
}

fn bench_clone_from_10_0010_0000(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 10, 0)
}

fn bench_clone_from_10_0100_0010(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 100, 10)
}

fn bench_clone_from_10_1000_0100(b: &mut Bencher) {
    do_bench_clone_from(b, 10, 1000, 100)
}

macro_rules! bench_in_place {
    ($($fname:ident, $type:ty, $count:expr, $init:expr);*) => {
        $(

            fn $fname(b: &mut Bencher) {
                b.iter(|| {
                    let src: Vec<$type> = black_box(vec![$init; $count]);
                    src.into_iter()
                        .enumerate()
                        .map(|(idx, e)| idx as $type ^ e)
                        .collect::<Vec<$type>>()
                });
            }
        )+
    };
}

bench_in_place![
    bench_in_place_xxu8_0010_i0,   u8,   10, 0;
    bench_in_place_xxu8_0100_i0,   u8,  100, 0;
    bench_in_place_xxu8_1000_i0,   u8, 1000, 0;
    bench_in_place_xxu8_0010_i1,   u8,   10, 1;
    bench_in_place_xxu8_0100_i1,   u8,  100, 1;
    bench_in_place_xxu8_1000_i1,   u8, 1000, 1;
    bench_in_place_xu32_0010_i0,  u32,   10, 0;
    bench_in_place_xu32_0100_i0,  u32,  100, 0;
    bench_in_place_xu32_1000_i0,  u32, 1000, 0;
    bench_in_place_xu32_0010_i1,  u32,   10, 1;
    bench_in_place_xu32_0100_i1,  u32,  100, 1;
    bench_in_place_xu32_1000_i1,  u32, 1000, 1;
    bench_in_place_u128_0010_i0, u128,   10, 0;
    bench_in_place_u128_0100_i0, u128,  100, 0;
    bench_in_place_u128_1000_i0, u128, 1000, 0;
    bench_in_place_u128_0010_i1, u128,   10, 1;
    bench_in_place_u128_0100_i1, u128,  100, 1;
    bench_in_place_u128_1000_i1, u128, 1000, 1
];

fn bench_in_place_recycle(b: &mut Bencher) {
    let mut data = vec![0; 1000];

    b.iter(|| {
        let tmp = core::mem::take(&mut data);
        data = black_box(
            tmp.into_iter()
                .enumerate()
                .map(|(idx, e)| idx.wrapping_add(e))
                .fuse()
                .collect::<Vec<usize>>(),
        );
    });
}

// fn bench_in_place_zip_recycle(b: &mut Bencher) {
//     let mut data = vec![0u8; 1000];
//     let mut rng = rand::thread_rng();
//     let mut subst = vec![0u8; 1000];
//     rng.fill_bytes(&mut subst[..]);

//     b.iter(|| {
//         let tmp = std::mem::take(&mut data);
//         let mangled = tmp
//             .into_iter()
//             .zip(subst.iter().copied())
//             .enumerate()
//             .map(|(i, (d, s))| d.wrapping_add(i as u8) ^ s)
//             .collect::<Vec<_>>();
//         data = black_box(mangled);
//     });
// }

// fn bench_in_place_zip_iter_mut(b: &mut Bencher) {
//     let mut data = vec![0u8; 256];
//     let mut rng = rand::thread_rng();
//     let mut subst = vec![0u8; 1000];
//     rng.fill_bytes(&mut subst[..]);

//     b.iter(|| {
//         data.iter_mut().enumerate().for_each(|(i, d)| {
//             *d = d.wrapping_add(i as u8) ^ subst[i];
//         });
//     });

//     black_box(data);
// }

pub fn vec_cast<T, U>(input: Vec<T>) -> Vec<U> {
    input
        .into_iter()
        .map(|e| unsafe { core::mem::transmute_copy(&e) })
        .collect()
}

fn bench_transmute(b: &mut Bencher) {
    let mut vec = vec![10u32; 100];
    b.bytes = 800; // 2 casts x 4 bytes x 100
    b.iter(|| {
        let v = core::mem::take(&mut vec);
        let v = black_box(vec_cast::<u32, i32>(v));
        let v = black_box(vec_cast::<i32, u32>(v));
        vec = v;
    });
}

#[derive(Clone)]
struct Droppable(usize);

impl Drop for Droppable {
    fn drop(&mut self) {
        black_box(self);
    }
}

fn bench_in_place_collect_droppable(b: &mut Bencher) {
    let v: Vec<Droppable> = core::iter::repeat_with(|| Droppable(0))
        .take(1000)
        .collect();
    b.iter(|| {
        v.clone()
            .into_iter()
            .skip(100)
            .enumerate()
            .map(|(i, e)| Droppable(i ^ e.0))
            .collect::<Vec<_>>()
    })
}

const LEN: usize = 16384;

fn bench_chain_collect(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| data.iter().cloned().chain([1]).collect::<Vec<_>>());
}

fn bench_chain_chain_collect(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        data.iter()
            .cloned()
            .chain([1])
            .chain([2])
            .collect::<Vec<_>>()
    });
}

fn bench_nest_chain_chain_collect(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        data.iter()
            .cloned()
            .chain([1].iter().chain([2].iter()).cloned())
            .collect::<Vec<_>>()
    });
}

fn bench_range_map_collect(b: &mut Bencher) {
    b.iter(|| (0..LEN).map(|_| u32::default()).collect::<Vec<_>>());
}

fn bench_chain_extend_ref(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::with_capacity(data.len() + 1);
        v.extend(data.iter().chain([1].iter()));
        v
    });
}

fn bench_chain_extend_value(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::with_capacity(data.len() + 1);
        v.extend(data.iter().cloned().chain(Some(1)));
        v
    });
}

fn bench_rev_1(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::new();
        v.extend(data.iter().rev());
        v
    });
}

fn bench_rev_2(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::with_capacity(data.len());
        v.extend(data.iter().rev());
        v
    });
}

fn bench_map_regular(b: &mut Bencher) {
    let data = black_box([(0, 0); LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::new();
        v.extend(data.iter().map(|t| t.1));
        v
    });
}

fn bench_map_fast(b: &mut Bencher) {
    let data = black_box([(0, 0); LEN]);
    b.iter(|| {
        let mut result = Vec::with_capacity(data.len());
        for i in 0..data.len() {
            unsafe {
                *result.get_unchecked_mut(i) = data[i].0;
                result.set_len(i);
            }
        }
        result
    });
}

fn random_sorted_fill(mut seed: u32, buf: &mut [u32]) {
    let mask = if buf.len() < 8192 {
        0xFF
    } else if buf.len() < 200_000 {
        0xFFFF
    } else {
        0xFFFF_FFFF
    };

    for item in buf.iter_mut() {
        seed ^= seed << 13;
        seed ^= seed >> 17;
        seed ^= seed << 5;

        *item = seed & mask;
    }

    buf.sort();
}

fn bench_vec_dedup_old(b: &mut Bencher, sz: usize) {
    let mut template = vec![0u32; sz];
    b.bytes = core::mem::size_of_val(template.as_slice()) as u64;
    random_sorted_fill(0x43, &mut template);

    let mut vec = template.clone();
    b.iter(|| {
        let len = {
            let (dedup, _) = vec.partition_dedup();
            dedup.len()
        };
        vec.truncate(len);

        black_box(vec.first());
        vec.clear();
        vec.extend_from_slice(&template);
    });
}

fn bench_vec_dedup_new(b: &mut Bencher, sz: usize) {
    let mut template = vec![0u32; sz];
    b.bytes = core::mem::size_of_val(template.as_slice()) as u64;
    random_sorted_fill(0x43, &mut template);

    let mut vec = template.clone();
    b.iter(|| {
        vec.dedup();
        black_box(vec.first());
        vec.clear();
        vec.extend_from_slice(&template);
    });
}

fn bench_dedup_old_100(b: &mut Bencher) {
    bench_vec_dedup_old(b, 100);
}

fn bench_dedup_new_100(b: &mut Bencher) {
    bench_vec_dedup_new(b, 100);
}

fn bench_dedup_old_1000(b: &mut Bencher) {
    bench_vec_dedup_old(b, 1000);
}

fn bench_dedup_new_1000(b: &mut Bencher) {
    bench_vec_dedup_new(b, 1000);
}

fn bench_dedup_old_10000(b: &mut Bencher) {
    bench_vec_dedup_old(b, 10000);
}

fn bench_dedup_new_10000(b: &mut Bencher) {
    bench_vec_dedup_new(b, 10000);
}

fn bench_dedup_old_100000(b: &mut Bencher) {
    bench_vec_dedup_old(b, 100000);
}

fn bench_dedup_new_100000(b: &mut Bencher) {
    bench_vec_dedup_new(b, 100000);
}

struct RustMinimal {
    message: String,
}

macro_rules! run_kernel_bench {
    ($bencher:ident, $benchmark:ident) => {{
        $bencher.set_benchmark(stringify!($benchmark));
        $benchmark(&mut $bencher);
    }};
}

const RFL_SEMANTIC_PROBE_TYPE_ID: u64 = 0x5246_4c55_4e49_5459;
const RFL_SEMANTIC_PROBE_MODULE_ID: u64 = 0x5246_4c4b_4552_4e4c;
const RFL_SEMANTIC_PROBE_CALLSITE: u64 = 0x5246_4c43_414c_4c01;
const RFL_SEMANTIC_MISMATCH_TYPE_ID: u64 = 0x5246_4c55_4e49_54ff;
const RFL_SEMANTIC_MISMATCH_MODULE_ID: u64 = 0x5246_4c4b_4552_4eff;
const RFL_SEMANTIC_MISMATCH_CALLSITE: u64 = 0x5246_4c43_414c_4cff;

fn empty_semantic_type_stats_row() -> unialloc_bridge::SemanticTypeStatsSnapshot {
    unialloc_bridge::SemanticTypeStatsSnapshot::empty()
}

fn run_semantic_bridge_probe() {
    unialloc_bridge::reset_semantic_stats();
    let size = 64usize;
    let align = core::mem::align_of::<u64>();
    let flags = unialloc_bridge::UNIALLOC_FLAG_TYPE_ISOLATED;
    let ptr = unsafe {
        unialloc_bridge::semantic_alloc_with_metadata(
            size,
            align,
            RFL_SEMANTIC_PROBE_TYPE_ID,
            RFL_SEMANTIC_PROBE_MODULE_ID,
            flags,
            RFL_SEMANTIC_PROBE_CALLSITE,
        )
    };
    if ptr.is_null() {
        pr_alert!("UniAlloc semantic bridge probe allocation failed\n");
        return;
    }
    unsafe {
        core::ptr::write_bytes(ptr, 0xA5, size);
    }
    let dealloc_ok = unsafe {
        unialloc_bridge::semantic_dealloc_with_metadata(
            ptr,
            size,
            align,
            RFL_SEMANTIC_PROBE_TYPE_ID,
            RFL_SEMANTIC_PROBE_MODULE_ID,
            flags,
            RFL_SEMANTIC_PROBE_CALLSITE,
        )
    };

    let mismatch_ptr = unsafe {
        unialloc_bridge::semantic_alloc_with_metadata(
            size,
            align,
            RFL_SEMANTIC_PROBE_TYPE_ID,
            RFL_SEMANTIC_PROBE_MODULE_ID,
            flags,
            RFL_SEMANTIC_PROBE_CALLSITE,
        )
    };
    let mut mismatched_dealloc_ok = false;
    if mismatch_ptr.is_null() {
        pr_alert!("UniAlloc semantic metadata validation probe allocation failed\n");
    } else {
        unsafe {
            core::ptr::write_bytes(mismatch_ptr, 0x5A, size);
        }
        mismatched_dealloc_ok = unsafe {
            unialloc_bridge::semantic_dealloc_with_metadata(
                mismatch_ptr,
                size,
                align,
                RFL_SEMANTIC_MISMATCH_TYPE_ID,
                RFL_SEMANTIC_MISMATCH_MODULE_ID,
                flags,
                RFL_SEMANTIC_MISMATCH_CALLSITE,
            )
        };
    }

    let mut rows = [empty_semantic_type_stats_row(); 4];
    let mut type_stats_rows = 0usize;
    let mut probe_row_matched = false;
    if let Some(row_count) = unialloc_bridge::semantic_type_stats_snapshot(&mut rows) {
        type_stats_rows = row_count;
        let copied = core::cmp::min(row_count, rows.len());
        for row in rows[..copied].iter() {
            if row.type_id == RFL_SEMANTIC_PROBE_TYPE_ID
                && row.module_id == RFL_SEMANTIC_PROBE_MODULE_ID
                && row.callsite == RFL_SEMANTIC_PROBE_CALLSITE
                && row.allocations > 0
                && row.deallocations > 0
            {
                probe_row_matched = true;
            }
        }
    }

    if let Some(stats) = unialloc_bridge::semantic_stats_snapshot() {
        pr_info!(
            "UniAlloc semantic bridge probe: dealloc_ok={} allocator_total_allocations={} allocator_total_deallocations={} allocator_typed_allocations={} allocator_typed_deallocations={} allocator_fallback_allocations={} allocator_total_allocated_bytes={} allocator_typed_allocated_bytes={} allocator_coverage_basis_points={} allocator_type_stats_rows={} allocator_type_stats_dropped_events={} allocator_type_stats_probe_matched={}\n",
            dealloc_ok,
            stats.total_allocations,
            stats.total_deallocations,
            stats.typed_allocations,
            stats.typed_deallocations,
            stats.fallback_allocations,
            stats.total_allocated_bytes,
            stats.typed_allocated_bytes,
            stats.coverage_basis_points,
            type_stats_rows,
            stats.semantic_type_stats_dropped_events,
            probe_row_matched
        );
    } else {
        pr_alert!("UniAlloc semantic bridge probe stats snapshot ABI mismatch or copy failed\n");
    }

    if let Some(validation) = unialloc_bridge::semantic_metadata_validation_snapshot() {
        pr_info!(
            "UniAlloc semantic metadata validation: matched_dealloc_ok={} mismatched_dealloc_ok={} recovery_identity_matches={} recovery_identity_mismatches={} last_mismatch_requested_type_id={} last_mismatch_recorded_type_id={} last_mismatch_requested_module_id={} last_mismatch_recorded_module_id={} last_mismatch_requested_callsite={} last_mismatch_recorded_callsite={}\n",
            dealloc_ok,
            mismatched_dealloc_ok,
            validation.recovery_identity_matches,
            validation.recovery_identity_mismatches,
            validation.last_mismatch_requested_type_id,
            validation.last_mismatch_recorded_type_id,
            validation.last_mismatch_requested_module_id,
            validation.last_mismatch_recorded_module_id,
            validation.last_mismatch_requested_callsite,
            validation.last_mismatch_recorded_callsite
        );
    } else {
        pr_alert!("UniAlloc semantic metadata validation snapshot ABI mismatch or copy failed\n");
    }

    let c_abi = unialloc_bridge::run_c_abi_probe();
    if let Some(sample) =
        unialloc_bridge::constrained_boot_sample(1, c_abi, type_stats_rows, probe_row_matched)
    {
        pr_info!(
            "UNIALLOC_CONSTRAINED_BOOT_SAMPLE platform=rust-for-linux boot_cycle={} fixed_heap_ready={} c_abi_invoked={} c_abi_ready_after_init={} c_abi_round_trips={} c_abi_invalid_layouts_rejected={} c_abi_over_page_alignment_checked={} c_abi_over_page_alignment={} allocator_total_allocations={} allocator_total_deallocations={} allocator_typed_allocations={} allocator_typed_deallocations={} allocator_fallback_allocations={} allocator_fallback_deallocations={} allocator_total_allocated_bytes={} allocator_typed_allocated_bytes={} allocator_fallback_allocated_bytes={} allocator_coverage_basis_points={} allocator_type_stats_rows={} allocator_type_stats_dropped_events={} allocator_type_stats_probe_matched={} c_abi_probe_passed={}\n",
            sample.boot_cycle,
            sample.fixed_heap_ready,
            sample.c_abi_invoked,
            sample.c_abi_ready_after_init,
            sample.c_abi_round_trips,
            sample.c_abi_invalid_layouts_rejected,
            sample.c_abi_over_page_alignment_checked,
            sample.c_abi_over_page_alignment,
            sample.allocator_total_allocations,
            sample.allocator_total_deallocations,
            sample.allocator_typed_allocations,
            sample.allocator_typed_deallocations,
            sample.allocator_fallback_allocations,
            sample.allocator_fallback_deallocations,
            sample.allocator_total_allocated_bytes,
            sample.allocator_typed_allocated_bytes,
            sample.allocator_fallback_allocated_bytes,
            sample.allocator_coverage_basis_points,
            sample.allocator_type_stats_rows,
            sample.allocator_type_stats_dropped_events,
            sample.allocator_type_stats_probe_matched,
            c_abi.passed
        );
    } else {
        pr_alert!("UniAlloc constrained boot sample ABI mismatch or copy failed\n");
    }
}

impl KernelModule for RustMinimal {
    fn init() -> Result<Self> {
        let unialloc_ready = unialloc_bridge::ensure_initialized();
        pr_info!("Rust simple benchmarking\n");
        pr_info!("Am I built-in? {}\n", !cfg!(MODULE));
        pr_info!("UniAlloc fixed-heap bridge ready? {}\n", unialloc_ready);
        pr_info!(
            "UniAlloc fixed-heap committed bytes: {}\n",
            unialloc_bridge::committed_bytes()
        );
        if !unialloc_ready {
            pr_alert!("UniAlloc fixed-heap bridge initialization failed\n");
            return Err(Error::EINVAL);
        }
        let (stats_abi_version, stats_snapshot_size) =
            unialloc_bridge::semantic_stats_snapshot_abi();
        pr_info!(
            "UniAlloc semantic stats ABI: version={} snapshot_size={}\n",
            stats_abi_version,
            stats_snapshot_size
        );
        let (type_stats_abi_version, type_stats_record_size) =
            unialloc_bridge::semantic_type_stats_snapshot_abi();
        pr_info!(
            "UniAlloc semantic type-stats ABI: version={} record_size={}\n",
            type_stats_abi_version,
            type_stats_record_size
        );
        let (fallback_attr_abi_version, fallback_attr_snapshot_size) =
            unialloc_bridge::semantic_fallback_attribution_snapshot_abi();
        pr_info!(
            "UniAlloc semantic fallback attribution ABI: version={} snapshot_size={}\n",
            fallback_attr_abi_version,
            fallback_attr_snapshot_size
        );
        let (metadata_validation_abi_version, metadata_validation_snapshot_size) =
            unialloc_bridge::semantic_metadata_validation_snapshot_abi();
        pr_info!(
            "UniAlloc semantic metadata validation ABI: version={} snapshot_size={}\n",
            metadata_validation_abi_version,
            metadata_validation_snapshot_size
        );
        run_semantic_bridge_probe();
        unialloc_bridge::reset_semantic_stats();

        let mut b = Bencher {
            res: 0,
            bytes: 0,
            benchmark: "<unset>",
        };
        run_kernel_bench!(b, bench_new);
        run_kernel_bench!(b, bench_with_capacity_0000);
        run_kernel_bench!(b, bench_with_capacity_0010);
        run_kernel_bench!(b, bench_with_capacity_0100);
        run_kernel_bench!(b, bench_with_capacity_1000);
        run_kernel_bench!(b, bench_from_fn_0000);
        run_kernel_bench!(b, bench_from_fn_0010);
        run_kernel_bench!(b, bench_from_fn_0100);
        run_kernel_bench!(b, bench_from_fn_1000);
        run_kernel_bench!(b, bench_from_elem_0000);
        run_kernel_bench!(b, bench_from_elem_0010);
        run_kernel_bench!(b, bench_from_elem_0100);
        run_kernel_bench!(b, bench_from_elem_1000);
        run_kernel_bench!(b, bench_from_slice_0000);
        run_kernel_bench!(b, bench_from_slice_0010);
        run_kernel_bench!(b, bench_from_slice_0100);
        run_kernel_bench!(b, bench_from_slice_1000);
        run_kernel_bench!(b, bench_from_iter_0000);
        run_kernel_bench!(b, bench_from_iter_0010);
        run_kernel_bench!(b, bench_from_iter_0100);
        run_kernel_bench!(b, bench_from_iter_1000);
        run_kernel_bench!(b, bench_extend_0000_0000);
        run_kernel_bench!(b, bench_extend_0000_0010);
        run_kernel_bench!(b, bench_extend_0000_0100);
        run_kernel_bench!(b, bench_extend_0000_1000);
        run_kernel_bench!(b, bench_extend_0010_0010);
        run_kernel_bench!(b, bench_extend_0100_0100);
        run_kernel_bench!(b, bench_extend_1000_1000);
        run_kernel_bench!(b, bench_extend_recycle);
        run_kernel_bench!(b, bench_extend_from_slice_0000_0000);
        run_kernel_bench!(b, bench_extend_from_slice_0000_0010);
        run_kernel_bench!(b, bench_extend_from_slice_0000_0100);
        run_kernel_bench!(b, bench_extend_from_slice_0000_1000);
        run_kernel_bench!(b, bench_extend_from_slice_0010_0010);
        run_kernel_bench!(b, bench_extend_from_slice_0100_0100);
        run_kernel_bench!(b, bench_extend_from_slice_1000_1000);
        run_kernel_bench!(b, bench_clone_0000);
        run_kernel_bench!(b, bench_clone_0010);
        run_kernel_bench!(b, bench_clone_0100);
        run_kernel_bench!(b, bench_clone_1000);
        run_kernel_bench!(b, bench_clone_from_01_0000_0000);
        run_kernel_bench!(b, bench_clone_from_01_0000_0010);
        run_kernel_bench!(b, bench_clone_from_01_0000_0100);
        run_kernel_bench!(b, bench_clone_from_01_0000_1000);
        run_kernel_bench!(b, bench_clone_from_01_0010_0010);
        run_kernel_bench!(b, bench_clone_from_01_0100_0100);
        run_kernel_bench!(b, bench_clone_from_01_1000_1000);
        run_kernel_bench!(b, bench_clone_from_01_0010_0100);
        run_kernel_bench!(b, bench_clone_from_01_0100_1000);
        run_kernel_bench!(b, bench_clone_from_01_0010_0000);
        run_kernel_bench!(b, bench_clone_from_01_0100_0010);
        run_kernel_bench!(b, bench_clone_from_01_1000_0100);
        run_kernel_bench!(b, bench_clone_from_10_0000_0000);
        run_kernel_bench!(b, bench_clone_from_10_0000_0010);
        run_kernel_bench!(b, bench_clone_from_10_0000_0100);
        run_kernel_bench!(b, bench_clone_from_10_0000_1000);
        run_kernel_bench!(b, bench_clone_from_10_0010_0010);
        run_kernel_bench!(b, bench_clone_from_10_0100_0100);
        run_kernel_bench!(b, bench_clone_from_10_1000_1000);
        run_kernel_bench!(b, bench_clone_from_10_0010_0100);
        run_kernel_bench!(b, bench_clone_from_10_0100_1000);
        run_kernel_bench!(b, bench_clone_from_10_0010_0000);
        run_kernel_bench!(b, bench_clone_from_10_0100_0010);
        run_kernel_bench!(b, bench_clone_from_10_1000_0100);
        run_kernel_bench!(b, bench_in_place_xxu8_0010_i0);
        run_kernel_bench!(b, bench_in_place_xxu8_0100_i0);
        run_kernel_bench!(b, bench_in_place_xxu8_1000_i0);
        run_kernel_bench!(b, bench_in_place_xxu8_0010_i1);
        run_kernel_bench!(b, bench_in_place_xxu8_0100_i1);
        run_kernel_bench!(b, bench_in_place_xxu8_1000_i1);
        run_kernel_bench!(b, bench_in_place_xu32_0010_i0);
        run_kernel_bench!(b, bench_in_place_xu32_0100_i0);
        run_kernel_bench!(b, bench_in_place_xu32_1000_i0);
        run_kernel_bench!(b, bench_in_place_xu32_0010_i1);
        run_kernel_bench!(b, bench_in_place_xu32_0100_i1);
        run_kernel_bench!(b, bench_in_place_xu32_1000_i1);
        run_kernel_bench!(b, bench_in_place_u128_0010_i0);
        run_kernel_bench!(b, bench_in_place_u128_0100_i0);
        run_kernel_bench!(b, bench_in_place_u128_1000_i0);
        run_kernel_bench!(b, bench_in_place_u128_0010_i1);
        run_kernel_bench!(b, bench_in_place_u128_0100_i1);
        run_kernel_bench!(b, bench_in_place_u128_1000_i1);
        run_kernel_bench!(b, bench_in_place_recycle);

        // bench_in_place_zip_recycle(&mut b);
        // bench_in_place_zip_iter_mut(&mut b);

        run_kernel_bench!(b, bench_transmute);
        run_kernel_bench!(b, bench_in_place_collect_droppable);
        // bench_chain_collect(&mut b);
        // bench_chain_chain_collect(&mut b);
        // bench_nest_chain_chain_collect(&mut b);
        // bench_range_map_collect(&mut b);
        // bench_chain_extend_ref(&mut b);
        // bench_chain_extend_value(&mut b);
        // bench_rev_1(&mut b);
        // bench_rev_2(&mut b);
        // bench_map_regular(&mut b);
        // bench_map_fast(&mut b);
        run_kernel_bench!(b, bench_dedup_old_100);
        run_kernel_bench!(b, bench_dedup_new_100);
        run_kernel_bench!(b, bench_dedup_old_1000);
        run_kernel_bench!(b, bench_dedup_new_1000);
        run_kernel_bench!(b, bench_dedup_old_10000);
        run_kernel_bench!(b, bench_dedup_new_10000);
        run_kernel_bench!(b, bench_dedup_old_100000);
        run_kernel_bench!(b, bench_dedup_new_100000);

        if let Some(stats) = unialloc_bridge::semantic_stats_snapshot() {
            pr_info!(
                "UniAlloc semantic stats: allocator_total_allocations={} allocator_total_deallocations={} allocator_typed_allocations={} allocator_fallback_allocations={} allocator_total_allocated_bytes={} allocator_coverage_basis_points={} allocator_type_stats_dropped_events={}\n",
                stats.total_allocations,
                stats.total_deallocations,
                stats.typed_allocations,
                stats.fallback_allocations,
                stats.total_allocated_bytes,
                stats.coverage_basis_points,
                stats.semantic_type_stats_dropped_events
            );
        } else {
            pr_alert!("UniAlloc semantic stats snapshot ABI mismatch or copy failed\n");
        }
        if let Some(fallback) = unialloc_bridge::semantic_fallback_attribution_snapshot() {
            pr_info!(
                "UniAlloc semantic fallback attribution: raw_alloc_no_metadata={} raw_dealloc_no_metadata={} raw_realloc_no_metadata={} raw_realloc_moved_dealloc_no_metadata={} realloc_recorded_old_metadata_new_allocations={}\n",
                fallback.raw_alloc_no_metadata,
                fallback.raw_dealloc_no_metadata,
                fallback.raw_realloc_no_metadata,
                fallback.raw_realloc_moved_dealloc_no_metadata,
                fallback.realloc_recorded_old_metadata_new_allocations
            );
        } else {
            pr_alert!(
                "UniAlloc semantic fallback attribution snapshot ABI mismatch or copy failed\n"
            );
        }
        if let Some(validation) = unialloc_bridge::semantic_metadata_validation_snapshot() {
            pr_info!(
                "UniAlloc semantic metadata validation final: recovery_identity_matches={} recovery_identity_mismatches={} last_mismatch_requested_type_id={} last_mismatch_recorded_type_id={}\n",
                validation.recovery_identity_matches,
                validation.recovery_identity_mismatches,
                validation.last_mismatch_requested_type_id,
                validation.last_mismatch_recorded_type_id
            );
        } else {
            pr_alert!("UniAlloc semantic metadata validation snapshot ABI mismatch or copy failed\n");
        }

        Ok(RustMinimal {
            message: "on the heap!".to_owned(),
        })
    }
}

impl Drop for RustMinimal {
    fn drop(&mut self) {
        pr_info!("My message is {}\n", self.message);
        pr_info!("Rust minimal sample (exit)\n");
    }
}
