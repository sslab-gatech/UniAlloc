mod allocator;

use proc_macro::TokenStream;

#[proc_macro]
pub fn atomic_static(ts: TokenStream) -> TokenStream {
    allocator::atomic_static(ts)
}

#[proc_macro]
pub fn tls_static(ts: TokenStream) -> TokenStream {
    allocator::tls_static(ts)
}

#[proc_macro]
pub fn generate_num_pages(ts: TokenStream) -> TokenStream {
    allocator::generate_num_pages(ts)
}
