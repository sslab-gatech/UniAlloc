// defining the allocator used in test
use unialloc::UniAlloc;
#[global_allocator]
static A: UniAlloc = UniAlloc;
