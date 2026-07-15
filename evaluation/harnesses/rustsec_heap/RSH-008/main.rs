//! RUSTSEC-2021-0130 / lru 0.7.0.
//!
//! The vulnerable iterator signature disconnects the yielded references from
//! the cache borrow.  This lets safe code remove and free an entry while a
//! reference into that entry remains live.

use lru::LruCache;
fn main() {
    let mut cache = LruCache::new(100);
    cache.put(1, String::from("Hello world"));
    cache.put(2, String::from("How are you?"));
    cache.put(3, String::from("It's a great day!"));

    for (key, value) in cache.iter() {
        cache.pop(key);
        println!("{value}");
    }
}
