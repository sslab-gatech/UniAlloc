let mut key = Key::variable(String::from("a"));
Key::variable("averylongkeywithlotsofletters")
    .set_st(YDB_NOTTP, Vec::new(), b"some val")
    .unwrap();
key.sub_next_self_st(YDB_NOTTP, Vec::new()).unwrap();
