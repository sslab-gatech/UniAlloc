use std::collections::HashMap;
use std::time::Duration;

extern crate alloc;
extern crate rand;

use unialloc::UniAlloc;
#[global_allocator]
static A: UniAlloc = UniAlloc;

use rand::distributions::Alphanumeric;
use rand::{thread_rng, Rng};

#[derive(Debug, Clone)]
struct BenchInput {
    size: usize,
    hit_ratio: f32,
    repetitions: u32,
}

fn bench_hashmap(input: &BenchInput) {
    let mut rng = rand::thread_rng();

    let mut container = HashMap::new();

    let mut buffer = Vec::new();
    for _ in 0..input.size {
        let rand_string: String = thread_rng()
            .sample_iter(&Alphanumeric)
            .take(12)
            .map(char::from)
            .collect();
        let rand_number: u64 = rng.gen();
        container.insert(rand_string.clone(), rand_number);
        buffer.push(rand_string);
    }

    let mut targets = Vec::with_capacity(input.repetitions as usize);
    for _ in 0..input.repetitions {
        let target = if rng.gen_range(0.0, 1.0) <= input.hit_ratio {
            let index = rng.gen_range(0, buffer.len());
            buffer[index].clone()
        } else {
            thread_rng()
                .sample_iter(&Alphanumeric)
                .take(12)
                .map(char::from)
                .collect()
        };
        targets.push(target);
    }

    for target in &targets {
        container.get(target);
    }
}
fn main() {
    let args = vec![
        BenchInput {
            size: 128,
            hit_ratio: 0.9,
            repetitions: 1000,
        },
        BenchInput {
            size: 64,
            hit_ratio: 0.9,
            repetitions: 1000,
        },
        BenchInput {
            size: 32,
            hit_ratio: 0.9,
            repetitions: 1000,
        },
        BenchInput {
            size: 16,
            hit_ratio: 0.9,
            repetitions: 1000,
        },
        BenchInput {
            size: 16,
            hit_ratio: 0.1,
            repetitions: 1000,
        },
        BenchInput {
            size: 8,
            hit_ratio: 0.9,
            repetitions: 1000,
        },
        BenchInput {
            size: 4,
            hit_ratio: 0.9,
            repetitions: 1000,
        },
    ];
    for item in args {
        bench_hashmap(&item);
    }
}
