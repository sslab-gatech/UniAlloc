obj-m += rust_bench.o
KERN_SRC ?= ~/rust-for-linux
KERNEL_MAKE_TARGET ?= modules
UNIALLOC_ROOT ?= ../../..
# The Rust-for-Linux C007 evidence parser requires positive UniAlloc allocator
# counters from the kernel module log.  Those counters are compiled behind the
# `stats` feature, so keep it on for the rlib dependency passed to the final
# module instead of relying on symbol presence alone.
UNIALLOC_FEATURES ?= fixed_heap,allow_mem_leak,stats
UNIALLOC_TOOLCHAIN ?= $(shell cat $(UNIALLOC_ROOT)/rust-toolchain 2>/dev/null || printf nightly)
UNIALLOC_CARGO ?= cargo +$(UNIALLOC_TOOLCHAIN)
UNIALLOC_TARGET_DIR ?= $(UNIALLOC_ROOT)/target/rust-for-linux-unialloc
UNIALLOC_RLIB ?= $(UNIALLOC_TARGET_DIR)/debug/libunialloc.rlib
UNIALLOC_DEPS_DIR ?= $(UNIALLOC_TARGET_DIR)/debug/deps

# Build UniAlloc as an rlib dependency of the final Rust-for-Linux module crate.
# A bare no_std allocator library cannot be linked as a standalone staticlib:
# the final kernel module owns the global allocator and panic runtime.  Passing
# the rlib through rustc's normal --extern path preserves that ownership and
# lets rust_bench.rs remain the single final crate.
$(UNIALLOC_RLIB):
	cd $(UNIALLOC_ROOT) && \
		$(UNIALLOC_CARGO) build -p unialloc --lib --no-default-features \
		--features $(UNIALLOC_FEATURES) \
		--target-dir $(UNIALLOC_TARGET_DIR)

all: $(UNIALLOC_RLIB)
	$(MAKE) -C $(KERN_SRC) M=$(CURDIR) $(KERNEL_MAKE_TARGET) CC=$(CC) \
		RUSTFLAGS_MODULE="$(RUSTFLAGS_MODULE) --extern unialloc=$(abspath $(UNIALLOC_RLIB)) -Ldependency=$(abspath $(UNIALLOC_DEPS_DIR))"

clean:
	$(MAKE) -C $(KERN_SRC) M=$(CURDIR) clean

.PHONY: all clean
