//! Allocator-visible root-cause adapter for Rudra-PoC 0105.
//!
//! Rudra recorded the affected `TransformContent` sites without a runnable
//! PoC. This adapter reaches the cited fixed-array transformation through the
//! public `ToRealTimeMatrix` API. A local storage conversion panics after the
//! vulnerable `ptr::read`, and its `Box<u64>` makes duplicate reclaim visible
//! to AddressSanitizer.

#![forbid(unsafe_code)]

use basic_dsp_matrix::ToRealTimeMatrix;
use basic_dsp_vector::{RealTimeVec, ToRealVector, ToSlice, VoidResult};

struct DropDetector {
    data: Vec<f32>,
    #[allow(dead_code)]
    payload: Box<u64>,
}

impl DropDetector {
    fn new(id: u64) -> Self {
        Self {
            data: vec![id as f32],
            payload: Box::new(id),
        }
    }
}

impl ToSlice<f32> for DropDetector {
    fn to_slice(&self) -> &[f32] {
        &self.data
    }

    fn len(&self) -> usize {
        self.data.len()
    }

    fn is_empty(&self) -> bool {
        self.data.is_empty()
    }

    fn alloc_len(&self) -> usize {
        self.data.capacity()
    }

    fn try_resize(&mut self, len: usize) -> VoidResult {
        self.data.resize(len, 0.0);
        Ok(())
    }
}

impl ToRealVector<f32> for DropDetector {
    fn to_real_time_vec(self) -> RealTimeVec<Self, f32> {
        panic!("published TransformContent conversion panic point")
    }

    fn to_real_freq_vec(self) -> basic_dsp_vector::RealFreqVec<Self, f32> {
        panic!("unused frequency conversion")
    }
}

fn main() {
    let values = [DropDetector::new(1), DropDetector::new(2)];
    let _ = <[DropDetector; 2] as ToRealTimeMatrix<
        RealTimeVec<DropDetector, f32>,
        f32,
    >>::to_real_time_mat(values);
}
