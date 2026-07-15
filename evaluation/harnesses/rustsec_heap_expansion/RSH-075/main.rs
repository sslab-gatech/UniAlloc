use diesel::dsl::sql;
use diesel::sql_types::{Integer, Text};
use diesel::sqlite::SqliteConnection;
use diesel::{Connection, RunQueryDsl};

fn main() {
    let mut source = SqliteConnection::establish(":memory:").unwrap();
    diesel::sql_query("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
        .execute(&mut source)
        .unwrap();
    diesel::sql_query(
        "INSERT INTO users (name, email) VALUES \
         ('John Doe', 'john.doe@example.com'), \
         ('Jane Doe', 'jane.doe@example.com')",
    )
    .execute(&mut source)
    .unwrap();

    let serialized = source.serialize_database_to_buffer();
    let mut target = SqliteConnection::establish(":memory:").unwrap();
    target
        .deserialize_readonly_database_from_buffer(serialized.as_slice())
        .unwrap();

    // This is the ordering added by Diesel's upstream regression test: release
    // the caller-owned buffer while SQLite still points at it, then query.
    drop(serialized);
    let rows = sql::<(Integer, Text, Text)>("SELECT id, name, email FROM users ORDER BY id")
        .load::<(i32, String, String)>(&mut target)
        .unwrap();
    assert_eq!(rows.len(), 2);
}
