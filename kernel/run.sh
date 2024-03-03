#!/bin/bash

set -euxo pipefail

CUR=`dirname $(realpath $0)`

if [[ ${1---x86} == "--arm" ]]; then
	QEMU_BIN=qemu-system-aarch64
	IMG=~/rust-for-linux/arch/arm64/boot/Image.gz
	CPIO=$CUR/arm.cpio

	./arm-mkfs.sh

	# to debug aarch64 kernel, please use gdb-multiarch
	# and add -s -S arguments to qemu
	$QEMU_BIN \
		-M virt,mte=on \
		-cpu max \
		-smp 1 \
		-kernel $IMG \
		-initrd $CPIO \
		-append "quiet" \
		-nographic -no-reboot

elif [[ ${1---x86} == "--pku" ]];then
	QEMU_BIN=qemu-system-x86_64
	IMG=~/rust-for-linux/arch/x86/boot/bzImage
	CPIO=$CUR/initrd.cpio


	./mkfs.sh
	
	$QEMU_BIN \
		-cpu qemu64,+pku,+xsave \
		-m 1G \
		-kernel $IMG \
		-initrd $CPIO \
		-append "console=ttyS0 quiet" \
		-nographic -monitor /dev/null -no-reboot
elif [[ ${1--x86} == "--ori" ]]; then
	QEMU_BIN=qemu-system-x86_64
	IMG=~/rust-for-linux-ori/arch/x86/boot/bzImage
	CPIO=$CUR/initrd.cpio


	./mkfs-ori.sh
	
	# kvm module requires root privilege
	sudo $QEMU_BIN \
		-cpu kvm64,vmx=on,-rdtscp \
		-m 1G \
		-kernel $IMG \
		-initrd $CPIO \
		-append "console=ttyS0 quiet" \
		-nographic -monitor /dev/null -no-reboot -enable-kvm

else
	QEMU_BIN=qemu-system-x86_64
	IMG=~/rust-for-linux/arch/x86/boot/bzImage
	CPIO=$CUR/initrd.cpio


	./mkfs.sh
	
	# kvm module requires root privilege
	sudo $QEMU_BIN \
		-cpu host \
		-m 1G \
		-kernel $IMG \
		-initrd $CPIO \
		-append "console=ttyS0 quiet" \
		-nographic -monitor /dev/null -no-reboot -enable-kvm
fi
