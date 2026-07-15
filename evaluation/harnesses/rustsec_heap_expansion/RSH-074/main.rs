use mutringbuf::ConcurrentHeapRB;

fn main() {
    // RUSTSEC-2026-0142's published payload, adapted only to mutringbuf
    // 1.0.0's public type name. 1024 Vec headers occupy 24,576 bytes on
    // x86_64, an exact multiple of the host's 4 KiB page size as required
    // by the vmem constructor.
    let values: Vec<Vec<u32>> = (0..1024).map(|i| vec![i, i + 1, i + 2]).collect();
    assert_eq!(
        std::mem::size_of_val(values.as_slice()) % mutringbuf::vmem_helper::page_size(),
        0
    );

    let ring: ConcurrentHeapRB<Vec<u32>> = ConcurrentHeapRB::from(values);
    drop(ring);
}
