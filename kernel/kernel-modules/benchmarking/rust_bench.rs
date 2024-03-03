#![no_std]
#![feature(allocator_api, global_asm)]
#![feature(asm)]
#![feature(slice_partition_dedup)]

#[macro_use]
extern crate alloc;

use kernel::prelude::*;
use alloc::boxed::Box;
use alloc::vec::Vec;
use kernel::bencher::{black_box, bench_it};
use core::iter::{repeat, FromIterator};


pub struct Bencher {
    res: u64,
    pub bytes: u64,
}


impl Bencher {

    pub fn iter<T, F>(&mut self, mut inner: F)
    where 
        F: FnMut() -> T,
    {
        let res: &mut [u64; 450]  = &mut [0u64; 450];

        for p in &mut *res {
            *p = bench_it(&mut inner); 
        }

        res.sort();;
        let mid = res.len() / 2;
        self.res = res[mid];
        pr_alert!("cycle: {}\n", self.res);
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
    let dst: Vec<_> = FromIterator::from_iter(0..src_len);
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
    input.into_iter().map(|e| unsafe { core::mem::transmute_copy(&e) }).collect()
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
    let v: Vec<Droppable> = core::iter::repeat_with(|| Droppable(0)).take(1000).collect();
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
    b.iter(|| data.iter().cloned().chain([1]).chain([2]).collect::<Vec<_>>());
}


fn bench_nest_chain_chain_collect(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        data.iter().cloned().chain([1].iter().chain([2].iter()).cloned()).collect::<Vec<_>>()
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

impl KernelModule for RustMinimal {
    fn init() -> Result<Self> {
        pr_info!("Rust simple benchmarking\n");
        pr_info!("Am I built-in? {}\n", !cfg!(MODULE));

        let mut b = Bencher {res: 0, bytes: 0};
        bench_new(&mut b);
        bench_with_capacity_0000(&mut b);
        bench_with_capacity_0010(&mut b);
        bench_with_capacity_0100(&mut b);
        bench_with_capacity_1000(&mut b);
        bench_from_fn_0000(&mut b);
        bench_from_fn_0010(&mut b);
        bench_from_fn_0100(&mut b);
        bench_from_fn_1000(&mut b);
        bench_from_elem_0000(&mut b);
        bench_from_elem_0010(&mut b);
        bench_from_elem_0100(&mut b);
        bench_from_elem_1000(&mut b);
        bench_from_slice_0000(&mut b);
        bench_from_slice_0010(&mut b);
        bench_from_slice_0100(&mut b);
        bench_from_slice_1000(&mut b);
        bench_from_iter_0000(&mut b);
        bench_from_iter_0010(&mut b);
        bench_from_iter_0100(&mut b);
        bench_from_iter_1000(&mut b);
        bench_extend_0000_0000(&mut b);
        bench_extend_0000_0010(&mut b);
        bench_extend_0000_0100(&mut b);
        bench_extend_0000_1000(&mut b);
        bench_extend_0010_0010(&mut b);
        bench_extend_0100_0100(&mut b);
        bench_extend_1000_1000(&mut b);
        bench_extend_recycle(&mut b);
        bench_extend_from_slice_0000_0000(&mut b);
        bench_extend_from_slice_0000_0010(&mut b);
        bench_extend_from_slice_0000_0100(&mut b);
        bench_extend_from_slice_0000_1000(&mut b);
        bench_extend_from_slice_0010_0010(&mut b);
        bench_extend_from_slice_0100_0100(&mut b);
        bench_extend_from_slice_1000_1000(&mut b);
        bench_clone_0000(&mut b);
        bench_clone_0010(&mut b);
        bench_clone_0100(&mut b);
        bench_clone_1000(&mut b);
        bench_clone_from_01_0000_0000(&mut b);
        bench_clone_from_01_0000_0010(&mut b);
        bench_clone_from_01_0000_0100(&mut b);
        bench_clone_from_01_0000_1000(&mut b);
        bench_clone_from_01_0010_0010(&mut b);
        bench_clone_from_01_0100_0100(&mut b);
        bench_clone_from_01_1000_1000(&mut b);
        bench_clone_from_01_0010_0100(&mut b);
        bench_clone_from_01_0100_1000(&mut b);
        bench_clone_from_01_0010_0000(&mut b);
        bench_clone_from_01_0100_0010(&mut b);
        bench_clone_from_01_1000_0100(&mut b);
        bench_clone_from_10_0000_0000(&mut b);
        bench_clone_from_10_0000_0010(&mut b);
        bench_clone_from_10_0000_0100(&mut b);
        bench_clone_from_10_0000_1000(&mut b);
        bench_clone_from_10_0010_0010(&mut b);
        bench_clone_from_10_0100_0100(&mut b);
        bench_clone_from_10_1000_1000(&mut b);
        bench_clone_from_10_0010_0100(&mut b);
        bench_clone_from_10_0100_1000(&mut b);
        bench_clone_from_10_0010_0000(&mut b);
        bench_clone_from_10_0100_0010(&mut b);
        bench_clone_from_10_1000_0100(&mut b);
        bench_in_place_xxu8_0010_i0(&mut b);
        bench_in_place_xxu8_0100_i0(&mut b);
        bench_in_place_xxu8_1000_i0(&mut b);
        bench_in_place_xxu8_0010_i1(&mut b);
        bench_in_place_xxu8_0100_i1(&mut b);
        bench_in_place_xxu8_1000_i1(&mut b);
        bench_in_place_xu32_0010_i0(&mut b);
        bench_in_place_xu32_0100_i0(&mut b);
        bench_in_place_xu32_1000_i0(&mut b);
        bench_in_place_xu32_0010_i1(&mut b);
        bench_in_place_xu32_0100_i1(&mut b);
        bench_in_place_xu32_1000_i1(&mut b);
        bench_in_place_u128_0010_i0(&mut b);
        bench_in_place_u128_0100_i0(&mut b);
        bench_in_place_u128_1000_i0(&mut b);
        bench_in_place_u128_0010_i1(&mut b);
        bench_in_place_u128_0100_i1(&mut b);
        bench_in_place_u128_1000_i1(&mut b);
        bench_in_place_recycle(&mut b);

        // bench_in_place_zip_recycle(&mut b);
        // bench_in_place_zip_iter_mut(&mut b);

        bench_transmute(&mut b);
        bench_in_place_collect_droppable(&mut b);
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
        bench_dedup_old_100(&mut b);
        bench_dedup_new_100(&mut b);
        bench_dedup_old_1000(&mut b);
        bench_dedup_new_1000(&mut b);
        bench_dedup_old_10000(&mut b);
        bench_dedup_new_10000(&mut b);
        bench_dedup_old_100000(&mut b);
        bench_dedup_new_100000(&mut b);


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

