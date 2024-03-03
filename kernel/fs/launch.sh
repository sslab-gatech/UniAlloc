#!/bin/sh

ROOT=/
FSSHARE_TAG="fsshare"
FSSHARE_PATH=$ROOT/queue

QEMU_BIN=/bin/qemu-system-x86_64
PC_BIOS=/usr/share/pc-bios
BOOT=$ROOT/boot
IMG=$BOOT/bzImage
CPIO=$BOOT/initrd.cpio

$QEMU_BIN \
	-L $PC_BIOS \
	-m 512M \
	-kernel $IMG \
	-initrd $CPIO \
	-append "console=ttyS0 quiet" \
	-fsdev local,security_model=mapped-file,id=fsdev9p,path=$FSSHARE_PATH \
	-device virtio-9p-pci,fsdev=fsdev9p,id=fs9p,mount_tag=$FSSHARE_TAG \
	-nographic -monitor /dev/null -no-reboot --enable-kvm
