use rand::RngCore;
use std::iter::{repeat, FromIterator};
use std::mem::size_of_val;
use test::{black_box, Bencher};

#[bench]
fn bench_new(b: &mut Bencher) {
    b.iter(|| Vec::<u32>::new())
}

macro_rules! bench_cases_1arg {
    ($runner:ident; $($name:ident => $arg:expr),+ $(,)?) => {
        $(
            #[bench]
            fn $name(b: &mut Bencher) {
                $runner(b, $arg)
            }
        )+
    };
}

macro_rules! bench_cases_2arg {
    ($runner:ident; $($name:ident => ($arg1:expr, $arg2:expr)),+ $(,)?) => {
        $(
            #[bench]
            fn $name(b: &mut Bencher) {
                $runner(b, $arg1, $arg2)
            }
        )+
    };
}

macro_rules! bench_cases_3arg {
    ($runner:ident; $($name:ident => ($arg1:expr, $arg2:expr, $arg3:expr)),+ $(,)?) => {
        $(
            #[bench]
            fn $name(b: &mut Bencher) {
                $runner(b, $arg1, $arg2, $arg3)
            }
        )+
    };
}

fn do_bench_with_capacity(b: &mut Bencher, src_len: usize) {
    b.bytes = src_len as u64;

    b.iter(|| Vec::<u32>::with_capacity(src_len))
}

bench_cases_1arg!(do_bench_with_capacity;
    bench_with_capacity_0000 => 0,
    bench_with_capacity_0010 => 10,
    bench_with_capacity_0100 => 100,
    bench_with_capacity_1000 => 1000,
);

fn do_bench_from_fn(b: &mut Bencher, src_len: usize) {
    b.bytes = src_len as u64;

    b.iter(|| (0..src_len).collect::<Vec<_>>())
}

bench_cases_1arg!(do_bench_from_fn;
    bench_from_fn_0000 => 0,
    bench_from_fn_0010 => 10,
    bench_from_fn_0100 => 100,
    bench_from_fn_1000 => 1000,
);

fn do_bench_from_elem(b: &mut Bencher, src_len: usize) {
    b.bytes = src_len as u64;

    b.iter(|| repeat(5).take(src_len).collect::<Vec<usize>>())
}

bench_cases_1arg!(do_bench_from_elem;
    bench_from_elem_0000 => 0,
    bench_from_elem_0010 => 10,
    bench_from_elem_0100 => 100,
    bench_from_elem_1000 => 1000,
);

fn do_bench_from_slice(b: &mut Bencher, src_len: usize) {
    let src: Vec<_> = FromIterator::from_iter(0..src_len);

    b.bytes = src_len as u64;

    b.iter(|| src.as_slice().to_vec());
}

bench_cases_1arg!(do_bench_from_slice;
    bench_from_slice_0000 => 0,
    bench_from_slice_0010 => 10,
    bench_from_slice_0100 => 100,
    bench_from_slice_1000 => 1000,
);

fn do_bench_from_iter(b: &mut Bencher, src_len: usize) {
    let src: Vec<_> = FromIterator::from_iter(0..src_len);

    b.bytes = src_len as u64;

    b.iter(|| {
        let dst: Vec<_> = FromIterator::from_iter(src.iter().cloned());
        dst
    });
}

bench_cases_1arg!(do_bench_from_iter;
    bench_from_iter_0000 => 0,
    bench_from_iter_0010 => 10,
    bench_from_iter_0100 => 100,
    bench_from_iter_1000 => 1000,
);

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

bench_cases_2arg!(do_bench_extend;
    bench_extend_0000_0000 => (0, 0),
    bench_extend_0000_0010 => (0, 10),
    bench_extend_0000_0100 => (0, 100),
    bench_extend_0000_1000 => (0, 1000),
    bench_extend_0010_0010 => (10, 10),
    bench_extend_0100_0100 => (100, 100),
    bench_extend_1000_1000 => (1000, 1000),
);

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

#[bench]
fn bench_extend_recycle(b: &mut Bencher) {
    let mut data = vec![0; 1000];

    b.iter(|| {
        let tmp = std::mem::take(&mut data);
        let mut to_extend = black_box(Vec::new());
        to_extend.extend(tmp.into_iter());
        data = black_box(to_extend);
    });

    black_box(data);
}

bench_cases_2arg!(do_bench_extend_from_slice;
    bench_extend_from_slice_0000_0000 => (0, 0),
    bench_extend_from_slice_0000_0010 => (0, 10),
    bench_extend_from_slice_0000_0100 => (0, 100),
    bench_extend_from_slice_0000_1000 => (0, 1000),
    bench_extend_from_slice_0010_0010 => (10, 10),
    bench_extend_from_slice_0100_0100 => (100, 100),
    bench_extend_from_slice_1000_1000 => (1000, 1000),
);

fn do_bench_clone(b: &mut Bencher, src_len: usize) {
    let src: Vec<usize> = FromIterator::from_iter(0..src_len);

    b.bytes = src_len as u64;

    b.iter(|| src.clone());
}

bench_cases_1arg!(do_bench_clone;
    bench_clone_0000 => 0,
    bench_clone_0010 => 10,
    bench_clone_0100 => 100,
    bench_clone_1000 => 1000,
);

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

bench_cases_3arg!(do_bench_clone_from;
    bench_clone_from_01_0000_0000 => (1, 0, 0),
    bench_clone_from_01_0000_0010 => (1, 0, 10),
    bench_clone_from_01_0000_0100 => (1, 0, 100),
    bench_clone_from_01_0000_1000 => (1, 0, 1000),
    bench_clone_from_01_0010_0010 => (1, 10, 10),
    bench_clone_from_01_0100_0100 => (1, 100, 100),
    bench_clone_from_01_1000_1000 => (1, 1000, 1000),
    bench_clone_from_01_0010_0100 => (1, 10, 100),
    bench_clone_from_01_0100_1000 => (1, 100, 1000),
    bench_clone_from_01_0010_0000 => (1, 10, 0),
    bench_clone_from_01_0100_0010 => (1, 100, 10),
    bench_clone_from_01_1000_0100 => (1, 1000, 100),
    bench_clone_from_10_0000_0000 => (10, 0, 0),
    bench_clone_from_10_0000_0010 => (10, 0, 10),
    bench_clone_from_10_0000_0100 => (10, 0, 100),
    bench_clone_from_10_0000_1000 => (10, 0, 1000),
    bench_clone_from_10_0010_0010 => (10, 10, 10),
    bench_clone_from_10_0100_0100 => (10, 100, 100),
    bench_clone_from_10_1000_1000 => (10, 1000, 1000),
    bench_clone_from_10_0010_0100 => (10, 10, 100),
    bench_clone_from_10_0100_1000 => (10, 100, 1000),
    bench_clone_from_10_0010_0000 => (10, 10, 0),
    bench_clone_from_10_0100_0010 => (10, 100, 10),
    bench_clone_from_10_1000_0100 => (10, 1000, 100),
);

macro_rules! bench_in_place {
    ($($fname:ident, $type:ty, $count:expr, $init:expr);*) => {
        $(
            #[bench]
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

#[bench]
fn bench_in_place_recycle(b: &mut Bencher) {
    let mut data = vec![0; 1000];

    b.iter(|| {
        let tmp = std::mem::take(&mut data);
        data = black_box(
            tmp.into_iter()
                .enumerate()
                .map(|(idx, e)| idx.wrapping_add(e))
                .fuse()
                .peekable()
                .collect::<Vec<usize>>(),
        );
    });
}

#[bench]
fn bench_in_place_zip_recycle(b: &mut Bencher) {
    let mut data = vec![0u8; 1000];
    let mut rng = crate::bench_rng();
    let mut subst = vec![0u8; 1000];
    rng.fill_bytes(&mut subst[..]);

    b.iter(|| {
        let tmp = std::mem::take(&mut data);
        let mangled = tmp
            .into_iter()
            .zip(subst.iter().copied())
            .enumerate()
            .map(|(i, (d, s))| d.wrapping_add(i as u8) ^ s)
            .collect::<Vec<_>>();
        data = black_box(mangled);
    });
}

#[bench]
fn bench_in_place_zip_iter_mut(b: &mut Bencher) {
    let mut data = vec![0u8; 256];
    let mut rng = crate::bench_rng();
    let mut subst = vec![0u8; 1000];
    rng.fill_bytes(&mut subst[..]);

    b.iter(|| {
        data.iter_mut().enumerate().for_each(|(i, d)| {
            *d = d.wrapping_add(i as u8) ^ subst[i];
        });
    });

    black_box(data);
}

pub fn vec_cast<T, U>(input: Vec<T>) -> Vec<U> {
    input
        .into_iter()
        .map(|e| unsafe { std::mem::transmute_copy(&e) })
        .collect()
}

#[bench]
fn bench_transmute(b: &mut Bencher) {
    let mut vec = vec![10u32; 100];
    b.bytes = 800; // 2 casts x 4 bytes x 100
    b.iter(|| {
        let v = std::mem::take(&mut vec);
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

#[bench]
fn bench_in_place_collect_droppable(b: &mut Bencher) {
    let v: Vec<Droppable> = std::iter::repeat_with(|| Droppable(0)).take(1000).collect();
    b.iter(|| {
        v.clone()
            .into_iter()
            .skip(100)
            .enumerate()
            .map(|(i, e)| Droppable(i ^ e.0))
            .collect::<Vec<_>>()
    })
}

// node.js gives out of memory error to use with length 1_100_000
#[cfg(target_os = "emscripten")]
const LEN: usize = 4096;

#[cfg(not(target_os = "emscripten"))]
const LEN: usize = 16384;

#[bench]
fn bench_chain_collect(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| data.iter().cloned().chain([1]).collect::<Vec<_>>());
}

#[bench]
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

#[bench]
fn bench_nest_chain_chain_collect(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        data.iter()
            .cloned()
            .chain([1].iter().chain([2].iter()).cloned())
            .collect::<Vec<_>>()
    });
}

#[bench]
fn bench_range_map_collect(b: &mut Bencher) {
    b.iter(|| (0..LEN).map(|_| u32::default()).collect::<Vec<_>>());
}

#[bench]
fn bench_chain_extend_ref(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::with_capacity(data.len() + 1);
        v.extend(data.iter().chain([1].iter()));
        v
    });
}

#[bench]
fn bench_chain_extend_value(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::with_capacity(data.len() + 1);
        v.extend(data.iter().cloned().chain(Some(1)));
        v
    });
}

#[bench]
fn bench_rev_1(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::new();
        v.extend(data.iter().rev());
        v
    });
}

#[bench]
fn bench_rev_2(b: &mut Bencher) {
    let data = black_box([0; LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::with_capacity(data.len());
        v.extend(data.iter().rev());
        v
    });
}

#[bench]
fn bench_map_regular(b: &mut Bencher) {
    let data = black_box([(0, 0); LEN]);
    b.iter(|| {
        let mut v = Vec::<u32>::new();
        v.extend(data.iter().map(|t| t.1));
        v
    });
}

#[bench]
fn bench_map_fast(b: &mut Bencher) {
    let data = black_box([(0, 0); LEN]);
    b.iter(|| {
        let mut result: Vec<u32> = Vec::with_capacity(data.len());
        for i in 0..data.len() {
            unsafe {
                *result.as_mut_ptr().add(i) = data[i].0;
                result.set_len(i + 1);
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

// Measures performance of slice dedup impl.
// This was used to justify separate implementation of dedup for Vec.
// This algorithm was used for Vecs prior to Rust 1.52.
fn bench_dedup_slice_truncate(b: &mut Bencher, sz: usize) {
    let mut template = vec![0u32; sz];
    b.bytes = size_of_val(template.as_slice()) as u64;
    random_sorted_fill(0x43, &mut template);

    let mut vec = template.clone();
    b.iter(|| {
        let vec = black_box(&mut vec);
        let len = {
            let (dedup, _) = vec.partition_dedup();
            dedup.len()
        };
        vec.truncate(len);

        black_box(vec.first());
        let vec = black_box(vec);
        vec.clear();
        vec.extend_from_slice(&template);
    });
}

// Measures performance of Vec::dedup on random data.
fn bench_vec_dedup_random(b: &mut Bencher, sz: usize) {
    let mut template = vec![0u32; sz];
    b.bytes = size_of_val(template.as_slice()) as u64;
    random_sorted_fill(0x43, &mut template);

    let mut vec = template.clone();
    b.iter(|| {
        let vec = black_box(&mut vec);
        vec.dedup();
        black_box(vec.first());
        let vec = black_box(vec);
        vec.clear();
        vec.extend_from_slice(&template);
    });
}

// Measures performance of Vec::dedup when there is no items removed
fn bench_vec_dedup_none(b: &mut Bencher, sz: usize) {
    let mut template = vec![0u32; sz];
    b.bytes = size_of_val(template.as_slice()) as u64;
    template.chunks_exact_mut(2).for_each(|w| {
        w[0] = black_box(0);
        w[1] = black_box(5);
    });

    let mut vec = template.clone();
    b.iter(|| {
        let vec = black_box(&mut vec);
        vec.dedup();
        black_box(vec.first());
        // Unlike other benches of `dedup`
        // this doesn't reinitialize vec
        // because we measure how efficient dedup is
        // when no memory written
    });
}

// Measures performance of Vec::dedup when there is all items removed
fn bench_vec_dedup_all(b: &mut Bencher, sz: usize) {
    let mut template = vec![0u32; sz];
    b.bytes = size_of_val(template.as_slice()) as u64;
    template.iter_mut().for_each(|w| {
        *w = black_box(0);
    });

    let mut vec = template.clone();
    b.iter(|| {
        let vec = black_box(&mut vec);
        vec.dedup();
        black_box(vec.first());
        let vec = black_box(vec);
        vec.clear();
        vec.extend_from_slice(&template);
    });
}

bench_cases_1arg!(bench_dedup_slice_truncate;
    bench_dedup_slice_truncate_100 => 100,
    bench_dedup_slice_truncate_1000 => 1_000,
    bench_dedup_slice_truncate_10000 => 10_000,
    bench_dedup_slice_truncate_100000 => 100_000,
);

bench_cases_1arg!(bench_vec_dedup_random;
    bench_dedup_random_100 => 100,
    bench_dedup_random_1000 => 1_000,
    bench_dedup_random_10000 => 10_000,
    bench_dedup_random_100000 => 100_000,
);

bench_cases_1arg!(bench_vec_dedup_none;
    bench_dedup_none_100 => 100,
    bench_dedup_none_1000 => 1_000,
    bench_dedup_none_10000 => 10_000,
    bench_dedup_none_100000 => 100_000,
);

bench_cases_1arg!(bench_vec_dedup_all;
    bench_dedup_all_100 => 100,
    bench_dedup_all_1000 => 1_000,
    bench_dedup_all_10000 => 10_000,
    bench_dedup_all_100000 => 100_000,
);

#[bench]
fn bench_flat_map_collect(b: &mut Bencher) {
    let v = vec![777u32; 500000];
    b.iter(|| {
        v.iter()
            .flat_map(|color| color.rotate_left(8).to_be_bytes())
            .collect::<Vec<_>>()
    });
}

/// Reference benchmark that `retain` has to compete with.
#[bench]
fn bench_retain_iter_100000(b: &mut Bencher) {
    let mut v = Vec::with_capacity(100000);

    b.iter(|| {
        let mut tmp = std::mem::take(&mut v);
        tmp.clear();
        tmp.extend(black_box(1..=100000));
        v = tmp.into_iter().filter(|x| x & 1 == 0).collect();
    });
}

#[bench]
fn bench_retain_100000(b: &mut Bencher) {
    let mut v = Vec::with_capacity(100000);

    b.iter(|| {
        v.clear();
        v.extend(black_box(1..=100000));
        v.retain(|x| x & 1 == 0)
    });
}

#[bench]
fn bench_retain_whole_100000(b: &mut Bencher) {
    let mut v = black_box(vec![826u32; 100000]);
    b.iter(|| v.retain(|x| *x == 826u32));
}

#[bench]
fn bench_next_chunk(b: &mut Bencher) {
    let v = vec![13u8; 2048];

    b.iter(|| {
        const CHUNK: usize = 8;

        let mut sum = [0u32; CHUNK];
        let mut iter = black_box(v.clone()).into_iter();

        while let Ok(chunk) = iter.next_chunk::<CHUNK>() {
            for i in 0..CHUNK {
                sum[i] += chunk[i] as u32;
            }
        }

        sum
    })
}
