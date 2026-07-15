use slice_ring_buffer::*;

fn main() {
    let slice_deque = slice_ring_buffer::from_elem(String::from("DF"), 10);
    let iter1 = SliceRingBuffer::into_iter(slice_deque);
    let iter2 = &iter1.clone();
}
