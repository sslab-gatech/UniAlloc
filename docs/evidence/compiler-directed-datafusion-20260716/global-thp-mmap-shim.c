#define _GNU_SOURCE
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>
#include <stddef.h>
#include <stdint.h>
#include <errno.h>

static void *raw_mmap(void *addr, size_t len, int prot, int flags, int fd, off_t off) {
    long result = syscall(SYS_mmap, addr, len, prot, flags, fd, off);
    if (result == -1) return MAP_FAILED;
    void *ptr = (void *)result;
    if ((flags & MAP_ANONYMOUS) && (flags & MAP_PRIVATE) && !(flags & MAP_HUGETLB) && len >= (2UL << 20)) {
        (void)syscall(SYS_madvise, ptr, len, MADV_HUGEPAGE);
    }
    return ptr;
}

void *mmap(void *addr, size_t len, int prot, int flags, int fd, off_t off) {
    return raw_mmap(addr, len, prot, flags, fd, off);
}

void *mmap64(void *addr, size_t len, int prot, int flags, int fd, off64_t off) {
    return raw_mmap(addr, len, prot, flags, fd, (off_t)off);
}
