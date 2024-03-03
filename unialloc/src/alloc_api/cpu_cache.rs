use crate::collections::concurrent::PerCpuCache;
use crate::prelude::*;

atomic_static! {
    pub static ref GLOBAL_CPU_CACHE: PerCpuCache = {
        let cache = PerCpuCache::new();
        cache.init();
        cache
    };
}
