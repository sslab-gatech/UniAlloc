#!/bin/bash

set -exuo

CUR=`dirname $(realpath $0)`

RUST_HELLO=$CUR/kernel-modules/rust-hello/

pushd $RUST_HELLO
	make -f Makefile.arm64 CC=clang-12
popd

cp $RUST_HELLO/rust_hello.ko $CUR/arm64fs

pushd arm64fs
	find . | cpio -o --quiet -R 0:0 -H newc > ../arm.cpio
popd
