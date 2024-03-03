#!/bin/bash

set -exuo

CUR=`dirname $(realpath $0)`

#if [ ! -d $CUR/fs ]; then
#    /bin/bash ./decompress.sh
#fi

RUST_HELLO=$CUR/kernel-modules/rust-hello
RUST_BENCH=$CUR/kernel-modules/benchmarking

pushd $RUST_HELLO
	make -f Makefile LLVM=1 CC=clang
popd

cp $RUST_HELLO/rust_hello.ko $CUR/fs

pushd $RUST_BENCH
	make -f Makefile LLVM=1 CC=clang
	cp $RUST_BENCH/rust_bench.ko $CUR/fs
popd



pushd fs
	find . | cpio -o --quiet -R 0:0 -H newc > ../initrd.cpio
popd
