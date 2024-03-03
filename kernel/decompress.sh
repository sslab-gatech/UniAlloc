#!/bin/bash

rm -rf ./fs
mkdir fs

gunzip -k initrd.cpio.gz

pushd fs
cpio -i < ../initrd.cpio
popd

rm -f ./initrd.cpio
