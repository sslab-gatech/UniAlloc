#!/bin/bash

set -euxo pipefail

CUR=`dirname $(realpath $0)`
NCPU=`grep -c ^processor /proc/cpuinfo`

LINUX=$HOME
NAME="rust-for-linux"
LINUX_PATH=$LINUX/$NAME

rustup default nightly-2021-05-29
rustup component add rust-src

if [[ -d "$LINUX_PATH" ]]; then
	if [[ ${1---x86} == "--arm" ]]; then
		# aarch64
		pushd $LINUX_PATH
			if [ -f ./.config ]; then
				rm -f ./.config
			fi
	
			touch .config
			make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- rust.config
			make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- LLVM=1 -j32
		popd
	else 
		# x86_64
		pushd $LINUX_PATH
			if [ -f .config ]; then
				rm -f ./.config
			fi

			touch .config
			cp $CUR/x86_mini.config ./arch/x86/configs
	
			make ARCH=x86_64 -j32 CC=clang x86_mini.config LLVM=1
			make ARCH=x86_64 -j32 CC=clang kvm_guest.config LLVM=1
			make LLVM=1 -j32
		popd
	fi
else
	pushd $LINUX
		git clone --depth=1 git@gl.gtisc.gatech.edu:hanqing/rust-for-linux.git
	popd

	pushd $LINUX_PATH
		# build x86_64
		touch .config
		cp $CUR/x86_mini.config ./arch/x86/configs 
	
		make ARCH=x86_64 -j32 CC=clang x86_mini.config LLVM=1
		make ARCH=x86_64 -j32 CC=clang kvm_guest.config LLVM=1
		make LLVM=1 -j32
	
		# build aarch64
		rm .config
		touch .config
		make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- rust.config
		make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- LLVM=1 -j32
	popd
fi
