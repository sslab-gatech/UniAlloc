obj-m += rust_hello.o
KERN_SRC = ~/rust-for-linux

all:
	make -C ${KERN_SRC} M=$(PWD) modules CC=$(CC)

clean:
	make -C ${KERN_SRC} M=$(PWD) clean
