use std::collections::HashMap;
use std::time::Duration;

extern crate alloc;

use unialloc::UniAlloc;
#[global_allocator]
static A: UniAlloc = UniAlloc;

extern crate rand;

use rand::distributions::Alphanumeric;
use rand::{thread_rng, Rng};

#[derive(Debug, Clone)]
struct BenchInput {
    size: usize,
    hit_ratio: f32,
    repetitions: u32,
}

fn bench_vector(input: &BenchInput) {
    let mut rng = rand::thread_rng();

    let mut container: Vec<(String, u64)> = Vec::new();

    for _ in 0..input.size {
        let rand_string = thread_rng()
            .sample_iter(&Alphanumeric)
            .take(12)
            .map(char::from)
            .collect();
        let rand_number: u64 = rng.gen();
        container.push((rand_string, rand_number));
    }

    let mut targets = Vec::with_capacity(input.repetitions as usize);
    for _ in 0..input.repetitions {
        let target = if rng.gen_range(0.0, 1.0) <= input.hit_ratio {
            let index = rng.gen_range(0, container.len());
            container[index].0.clone()
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
        for (key, _value) in &container {
            if key == target {
                break;
            }
        }
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
        bench_vector(&item);
    }
}
