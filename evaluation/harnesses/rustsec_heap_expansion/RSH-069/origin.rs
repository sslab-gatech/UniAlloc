#[test]
#[cfg(ossl300)]
fn test_md_fetch_properties() {
    assert!(Md::fetch(None, "SHA-256", Some("provider=gibberish")).is_err());
}
