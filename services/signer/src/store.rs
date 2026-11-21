//! The signer's own data, in one SQLite file: accounts (who has which key, sealed, and their
//! username) and bunker connections. The register stays the source of truth for passes and key
//! versions; derived keys can be rebuilt from the custody seed, but connections (every signed-in
//! device), usernames and keys imported from Keycast that predate the seed live only here: the
//! backups must carry this file.

use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;

pub fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// How the signer holds an account's key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Custody {
    /// Derived from the custody seed; recoverable.
    Derived,
    /// Imported from Keycast (made before the seed); sealed only, back it up.
    Imported,
    /// The member holds their own key; the signer holds nothing.
    Own,
}

impl Custody {
    fn as_str(self) -> &'static str {
        match self {
            Custody::Derived => "derived",
            Custody::Imported => "imported",
            Custody::Own => "own",
        }
    }
    fn parse(s: &str) -> Custody {
        match s {
            "derived" => Custody::Derived,
            "imported" => Custody::Imported,
            _ => Custody::Own,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Account {
    pub vine_id: String,
    pub pubkey: String,
    pub custody: Custody,
    pub key_version: i64,
    pub status: String,
    /// The chosen username (the NIP-05 name), lowercase; unique among accounts.
    pub username: Option<String>,
    #[serde(skip)]
    pub sealed: Option<Vec<u8>>,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct ConnectionRow {
    pub id: String,
    pub vine_id: String,
    /// The application's name, as the member sees it in their sessions.
    pub app: String,
    pub label: String,
    pub bunker_pubkey: String,
    #[serde(skip)]
    pub bunker_sealed: Vec<u8>,
    /// SHA-256 (hex) of the URL's secret while the connection waits for its first `connect`;
    /// empty once a client is bound (and for a connection bound when it was made).
    #[serde(skip)]
    pub secret_hash: String,
    pub client_pubkey: Option<String>,
    pub derive: Vec<String>,
    pub created_at: i64,
    pub last_used_at: Option<i64>,
    /// Requests answered through this connection (API calls with `connection` included).
    pub uses: i64,
    /// While no client is bound: when the unclaimed connection stops existing.
    pub expires_at: Option<i64>,
}

pub struct Store {
    db: Mutex<Connection>,
}

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS accounts (
    vine_id     TEXT PRIMARY KEY,
    pubkey      TEXT NOT NULL,
    custody     TEXT NOT NULL CHECK (custody IN ('derived', 'imported', 'own')),
    key_version INTEGER NOT NULL DEFAULT 0,
    sealed      BLOB,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    username    TEXT
);
CREATE INDEX IF NOT EXISTS accounts_pubkey ON accounts (pubkey);
CREATE TABLE IF NOT EXISTS connections (
    id            TEXT PRIMARY KEY,
    vine_id       TEXT NOT NULL REFERENCES accounts (vine_id),
    app           TEXT NOT NULL,
    label         TEXT NOT NULL,
    bunker_pubkey TEXT NOT NULL UNIQUE,
    bunker_sealed BLOB NOT NULL,
    client_pubkey TEXT,
    derive        TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER,
    revoked_at    INTEGER,
    secret_hash   TEXT NOT NULL DEFAULT '',
    uses          INTEGER NOT NULL DEFAULT 0,
    expires_at    INTEGER
);
CREATE INDEX IF NOT EXISTS connections_vine_id ON connections (vine_id);
";

/// After the columns exist (a database made by the first version of the signer gets them in
/// `migrate`).
const INDEXES: &str = "
CREATE UNIQUE INDEX IF NOT EXISTS accounts_username ON accounts (username)
    WHERE username IS NOT NULL;
CREATE INDEX IF NOT EXISTS connections_client ON connections (client_pubkey);
";

fn account_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Account> {
    Ok(Account {
        vine_id: row.get(0)?,
        pubkey: row.get(1)?,
        custody: Custody::parse(&row.get::<_, String>(2)?),
        key_version: row.get(3)?,
        sealed: row.get(4)?,
        status: row.get(5)?,
        created_at: row.get(6)?,
        updated_at: row.get(7)?,
        username: row.get(8)?,
    })
}

const ACCOUNT_COLS: &str =
    "vine_id, pubkey, custody, key_version, sealed, status, created_at, updated_at, username";
const CONNECTION_COLS: &str = "id, vine_id, app, label, bunker_pubkey, bunker_sealed, \
     secret_hash, client_pubkey, derive, created_at, last_used_at, uses, expires_at";
/// A connection that exists: not revoked, and not an unclaimed one past its deadline.
const LIVE: &str = "revoked_at IS NULL AND NOT (client_pubkey IS NULL AND expires_at IS NOT NULL \
     AND expires_at <= ?1)";

fn connection_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ConnectionRow> {
    let derive: String = row.get(8)?;
    Ok(ConnectionRow {
        id: row.get(0)?,
        vine_id: row.get(1)?,
        app: row.get(2)?,
        label: row.get(3)?,
        bunker_pubkey: row.get(4)?,
        bunker_sealed: row.get(5)?,
        secret_hash: row.get(6)?,
        client_pubkey: row.get(7)?,
        derive: derive
            .split(',')
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect(),
        created_at: row.get(9)?,
        last_used_at: row.get(10)?,
        uses: row.get(11)?,
        expires_at: row.get(12)?,
    })
}

pub struct NewConnection<'a> {
    pub id: &'a str,
    pub vine_id: &'a str,
    pub app: &'a str,
    pub label: &'a str,
    pub bunker_pubkey: &'a str,
    pub bunker_sealed: &'a [u8],
    /// Empty with `client_pubkey` (bound from the start: no secret opens it).
    pub secret_hash: &'a str,
    pub client_pubkey: Option<&'a str>,
    pub derive: &'a [String],
    pub expires_at: Option<i64>,
}

/// What binding a client key to a connection came to.
#[derive(Debug, PartialEq, Eq)]
pub enum Bind {
    Bound,
    /// The connection already has a client (this one or another).
    Already(String),
    /// The client key is linked to another account: one client key, one identity.
    Taken,
    Gone,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Named {
    Set,
    Taken,
    NoAccount,
}

fn columns(conn: &Connection, table: &str) -> rusqlite::Result<Vec<String>> {
    let mut stmt = conn.prepare(&format!("SELECT name FROM pragma_table_info('{table}')"))?;
    let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
    rows.collect()
}

/// Whether `client_pubkey` is linked to an account other than `vine_id`: named for or bound to
/// any of its connections, revoked and expired ones included (a key is never freed).
fn client_of_another(
    conn: &Connection,
    vine_id: &str,
    client_pubkey: &str,
) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM connections WHERE client_pubkey = ?1 AND vine_id <> ?2)",
        params![client_pubkey, vine_id],
        |r| r.get(0),
    )
}

impl Store {
    pub fn open(path: &Path) -> rusqlite::Result<Self> {
        Self::init(Connection::open(path)?)
    }

    #[cfg(test)]
    pub fn in_memory() -> rusqlite::Result<Self> {
        Self::init(Connection::open_in_memory()?)
    }

    fn init(conn: Connection) -> rusqlite::Result<Self> {
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.execute_batch(SCHEMA)?;
        Self::migrate(&conn)?;
        conn.execute_batch(INDEXES)?;
        Ok(Self {
            db: Mutex::new(conn),
        })
    }

    /// A database made by the first version of the signer (PR #7): the username, the hashed
    /// secret, the use counter and the unclaimed deadline are added; a plain secret still
    /// waiting for its first connect is kept as its hash, then the column is dropped.
    fn migrate(conn: &Connection) -> rusqlite::Result<()> {
        if !columns(conn, "accounts")?.iter().any(|c| c == "username") {
            conn.execute_batch("ALTER TABLE accounts ADD COLUMN username TEXT")?;
        }
        let cols = columns(conn, "connections")?;
        let has = |name: &str| cols.iter().any(|c| c == name);
        if !has("secret_hash") {
            conn.execute_batch(
                "ALTER TABLE connections ADD COLUMN secret_hash TEXT NOT NULL DEFAULT ''",
            )?;
        }
        if !has("uses") {
            conn.execute_batch(
                "ALTER TABLE connections ADD COLUMN uses INTEGER NOT NULL DEFAULT 0",
            )?;
        }
        if !has("expires_at") {
            conn.execute_batch("ALTER TABLE connections ADD COLUMN expires_at INTEGER")?;
        }
        if has("secret") {
            let mut waiting: Vec<(String, String)> = Vec::new();
            {
                let mut stmt = conn.prepare(
                    "SELECT id, secret FROM connections
                     WHERE client_pubkey IS NULL AND revoked_at IS NULL AND secret <> ''",
                )?;
                let rows = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
                for row in rows {
                    waiting.push(row?);
                }
            }
            for (id, secret) in waiting {
                // they were made without a deadline; they keep none
                conn.execute(
                    "UPDATE connections SET secret_hash = ?2 WHERE id = ?1",
                    params![id, crate::signer::secret_hash(&secret)],
                )?;
            }
            conn.execute_batch("ALTER TABLE connections DROP COLUMN secret")?;
        }
        Ok(())
    }

    fn db(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.db.lock().unwrap_or_else(|e| e.into_inner())
    }

    // -- accounts --------------------------------------------------------------------------

    pub fn account(&self, vine_id: &str) -> rusqlite::Result<Option<Account>> {
        self.db()
            .query_row(
                &format!("SELECT {ACCOUNT_COLS} FROM accounts WHERE vine_id = ?1"),
                params![vine_id],
                account_from_row,
            )
            .optional()
    }

    pub fn accounts_by_pubkey(&self, pubkey: &str) -> rusqlite::Result<Vec<Account>> {
        let db = self.db();
        let mut stmt = db.prepare(&format!(
            "SELECT {ACCOUNT_COLS} FROM accounts WHERE pubkey = ?1 ORDER BY created_at, vine_id"
        ))?;
        let rows = stmt.query_map(params![pubkey], account_from_row)?;
        rows.collect()
    }

    pub fn account_by_username(&self, username: &str) -> rusqlite::Result<Option<Account>> {
        self.db()
            .query_row(
                &format!("SELECT {ACCOUNT_COLS} FROM accounts WHERE username = ?1"),
                params![username.to_ascii_lowercase()],
                account_from_row,
            )
            .optional()
    }

    /// Inserts the account, or replaces it (the key and how it is held; the status and the
    /// username are kept).
    pub fn put_account(
        &self,
        vine_id: &str,
        pubkey: &str,
        custody: Custody,
        key_version: i64,
        sealed: Option<&[u8]>,
    ) -> rusqlite::Result<()> {
        let t = now();
        self.db().execute(
            "INSERT INTO accounts (vine_id, pubkey, custody, key_version, sealed, status,
                                   created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, 'active', ?6, ?6)
             ON CONFLICT (vine_id) DO UPDATE SET pubkey = ?2, custody = ?3, key_version = ?4,
                 sealed = ?5, updated_at = ?6",
            params![vine_id, pubkey, custody.as_str(), key_version, sealed, t],
        )?;
        Ok(())
    }

    pub fn set_sealed(&self, vine_id: &str, sealed: &[u8]) -> rusqlite::Result<()> {
        self.db().execute(
            "UPDATE accounts SET sealed = ?2 WHERE vine_id = ?1",
            params![vine_id, sealed],
        )?;
        Ok(())
    }

    pub fn set_status(&self, vine_id: &str, status: &str) -> rusqlite::Result<bool> {
        let n = self.db().execute(
            "UPDATE accounts SET status = ?2, updated_at = ?3 WHERE vine_id = ?1",
            params![vine_id, status, now()],
        )?;
        Ok(n > 0)
    }

    /// The account's username (lowercase). One name, one account: the check and the write
    /// happen under the store's lock, and the unique index stands behind them.
    pub fn set_username(&self, vine_id: &str, username: &str) -> rusqlite::Result<Named> {
        let name = username.to_ascii_lowercase();
        let db = self.db();
        let holder: Option<String> = db
            .query_row(
                "SELECT vine_id FROM accounts WHERE username = ?1",
                params![name],
                |r| r.get(0),
            )
            .optional()?;
        match holder {
            Some(h) if h == vine_id => return Ok(Named::Set),
            Some(_) => return Ok(Named::Taken),
            None => {}
        }
        let n = db.execute(
            "UPDATE accounts SET username = ?2, updated_at = ?3 WHERE vine_id = ?1",
            params![vine_id, name, now()],
        )?;
        Ok(if n > 0 { Named::Set } else { Named::NoAccount })
    }

    /// Every sealed account key: (vine_id, pubkey, sealed), for the one-time seal upgrade.
    pub fn sealed_accounts(&self) -> rusqlite::Result<Vec<(String, String, Vec<u8>)>> {
        let db = self.db();
        let mut stmt =
            db.prepare("SELECT vine_id, pubkey, sealed FROM accounts WHERE sealed IS NOT NULL")?;
        let rows = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
        rows.collect()
    }

    /// Every connection's sealed bunker key: (id, bunker_pubkey, sealed), revoked ones too.
    pub fn sealed_connections(&self) -> rusqlite::Result<Vec<(String, String, Vec<u8>)>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT id, bunker_pubkey, bunker_sealed FROM connections")?;
        let rows = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
        rows.collect()
    }

    pub fn set_bunker_sealed(&self, id: &str, sealed: &[u8]) -> rusqlite::Result<()> {
        self.db().execute(
            "UPDATE connections SET bunker_sealed = ?2 WHERE id = ?1",
            params![id, sealed],
        )?;
        Ok(())
    }

    // -- connections -----------------------------------------------------------------------

    /// Adds the connection. With a client key: false, and nothing written, when that key is
    /// linked to another account (checked and written under one lock).
    pub fn add_connection(&self, c: &NewConnection<'_>) -> rusqlite::Result<bool> {
        let db = self.db();
        if let Some(client) = c.client_pubkey {
            if client_of_another(&db, c.vine_id, client)? {
                return Ok(false);
            }
        }
        db.execute(
            "INSERT INTO connections (id, vine_id, app, label, bunker_pubkey, bunker_sealed,
                                      secret_hash, client_pubkey, derive, created_at, expires_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                c.id,
                c.vine_id,
                c.app,
                c.label,
                c.bunker_pubkey,
                c.bunker_sealed,
                c.secret_hash,
                c.client_pubkey,
                c.derive.join(","),
                now(),
                c.expires_at,
            ],
        )?;
        Ok(true)
    }

    pub fn connections(&self, vine_id: &str) -> rusqlite::Result<Vec<ConnectionRow>> {
        let db = self.db();
        let mut stmt = db.prepare(&format!(
            "SELECT {CONNECTION_COLS} FROM connections
             WHERE vine_id = ?2 AND {LIVE} ORDER BY created_at DESC, rowid DESC"
        ))?;
        let rows = stmt.query_map(params![now(), vine_id], connection_from_row)?;
        rows.collect()
    }

    pub fn connection(&self, id: &str) -> rusqlite::Result<Option<ConnectionRow>> {
        self.db()
            .query_row(
                &format!("SELECT {CONNECTION_COLS} FROM connections WHERE id = ?2 AND {LIVE}"),
                params![now(), id],
                connection_from_row,
            )
            .optional()
    }

    pub fn connection_by_bunker(
        &self,
        bunker_pubkey: &str,
    ) -> rusqlite::Result<Option<ConnectionRow>> {
        self.db()
            .query_row(
                &format!(
                    "SELECT {CONNECTION_COLS} FROM connections
                     WHERE bunker_pubkey = ?2 AND {LIVE}"
                ),
                params![now(), bunker_pubkey],
                connection_from_row,
            )
            .optional()
    }

    /// The bunker keys the relay subscription asks for: every live connection's.
    pub fn live_bunker_pubkeys(&self) -> rusqlite::Result<Vec<String>> {
        let db = self.db();
        let mut stmt = db.prepare(&format!(
            "SELECT bunker_pubkey FROM connections WHERE {LIVE} ORDER BY bunker_pubkey"
        ))?;
        let rows = stmt.query_map(params![now()], |r| r.get(0))?;
        rows.collect()
    }

    /// Binds the connection to the client key that presented its secret: once, and never a
    /// key linked to another account. The secret is spent (its hash is dropped) and the
    /// unclaimed deadline lifted.
    pub fn bind_client(&self, id: &str, client_pubkey: &str) -> rusqlite::Result<Bind> {
        let db = self.db();
        let row: Option<(String, Option<String>)> = db
            .query_row(
                &format!("SELECT vine_id, client_pubkey FROM connections WHERE id = ?2 AND {LIVE}"),
                params![now(), id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let Some((vine_id, bound)) = row else {
            return Ok(Bind::Gone);
        };
        if let Some(bound) = bound {
            return Ok(Bind::Already(bound));
        }
        if client_of_another(&db, &vine_id, client_pubkey)? {
            return Ok(Bind::Taken);
        }
        db.execute(
            "UPDATE connections SET client_pubkey = ?2, secret_hash = '', expires_at = NULL,
                 last_used_at = ?3
             WHERE id = ?1 AND client_pubkey IS NULL",
            params![id, client_pubkey, now()],
        )?;
        Ok(Bind::Bound)
    }

    pub fn touch_connection(&self, id: &str) -> rusqlite::Result<()> {
        self.db().execute(
            "UPDATE connections SET last_used_at = ?2, uses = uses + 1 WHERE id = ?1",
            params![id, now()],
        )?;
        Ok(())
    }

    /// Revokes one connection of the account; false when there was none.
    pub fn revoke_connection(&self, vine_id: &str, id: &str) -> rusqlite::Result<bool> {
        let n = self.db().execute(
            "UPDATE connections SET revoked_at = ?3
             WHERE id = ?1 AND vine_id = ?2 AND revoked_at IS NULL",
            params![id, vine_id, now()],
        )?;
        Ok(n > 0)
    }

    /// Revokes the account's connection with this bunker key; false when there was none.
    pub fn revoke_by_bunker(&self, vine_id: &str, bunker_pubkey: &str) -> rusqlite::Result<bool> {
        let n = self.db().execute(
            "UPDATE connections SET revoked_at = ?3
             WHERE bunker_pubkey = ?1 AND vine_id = ?2 AND revoked_at IS NULL",
            params![bunker_pubkey, vine_id, now()],
        )?;
        Ok(n > 0)
    }

    /// Revokes the account's connections (of one app, or all); returns how many.
    pub fn revoke_connections(&self, vine_id: &str, app: Option<&str>) -> rusqlite::Result<usize> {
        let t = now();
        let db = self.db();
        match app {
            Some(app) => db.execute(
                "UPDATE connections SET revoked_at = ?3
                 WHERE vine_id = ?1 AND app = ?2 AND revoked_at IS NULL",
                params![vine_id, app, t],
            ),
            None => db.execute(
                "UPDATE connections SET revoked_at = ?2 WHERE vine_id = ?1 AND revoked_at IS NULL",
                params![vine_id, t],
            ),
        }
    }

    /// Revokes all but the newest `keep` connections of one app (silent sign-ins of Belfry's
    /// own apps make a new connection each time; old devices fall off).
    pub fn prune_connections(
        &self,
        vine_id: &str,
        app: &str,
        keep: usize,
    ) -> rusqlite::Result<usize> {
        self.db().execute(
            "UPDATE connections SET revoked_at = ?4
             WHERE vine_id = ?1 AND app = ?2 AND revoked_at IS NULL AND id NOT IN (
                 SELECT id FROM connections WHERE vine_id = ?1 AND app = ?2 AND revoked_at IS NULL
                 ORDER BY created_at DESC, rowid DESC LIMIT ?3)",
            params![vine_id, app, keep as i64, now()],
        )
    }

    /// Marks unclaimed connections past their deadline as revoked (they were already treated
    /// as gone); returns how many.
    pub fn expire_unclaimed(&self) -> rusqlite::Result<usize> {
        let t = now();
        self.db().execute(
            "UPDATE connections SET revoked_at = ?1
             WHERE revoked_at IS NULL AND client_pubkey IS NULL AND expires_at IS NOT NULL
               AND expires_at <= ?1",
            params![t],
        )
    }

    #[cfg(test)]
    pub fn set_expires_at(&self, id: &str, at: i64) {
        self.db()
            .execute(
                "UPDATE connections SET expires_at = ?2 WHERE id = ?1",
                params![id, at],
            )
            .unwrap();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn conn<'a>(id: &'a str, vine_id: &'a str, bunker: &'a str) -> NewConnection<'a> {
        NewConnection {
            id,
            vine_id,
            app: "belfry-wallet",
            label: "Wallet",
            bunker_pubkey: bunker,
            bunker_sealed: b"x",
            secret_hash: "h",
            client_pubkey: None,
            derive: &[],
            expires_at: None,
        }
    }

    #[test]
    fn accounts_and_connections_round_trip() {
        let s = Store::in_memory().unwrap();
        assert!(s.account("odoo-partner-1").unwrap().is_none());
        s.put_account("odoo-partner-1", "aa", Custody::Derived, 0, Some(b"sealed"))
            .unwrap();
        let a = s.account("odoo-partner-1").unwrap().unwrap();
        assert_eq!((a.custody, a.status.as_str()), (Custody::Derived, "active"));
        assert!(s.set_status("odoo-partner-1", "suspended").unwrap());
        assert!(!s.set_status("odoo-partner-2", "suspended").unwrap());
        // replacing the key keeps the status
        s.put_account("odoo-partner-1", "bb", Custody::Own, 0, None)
            .unwrap();
        let a = s.account("odoo-partner-1").unwrap().unwrap();
        assert_eq!((a.pubkey.as_str(), a.status.as_str()), ("bb", "suspended"));
        assert!(a.sealed.is_none());

        let derive = vec!["cashu-wallet-v1".to_string()];
        assert!(s
            .add_connection(&NewConnection {
                derive: &derive,
                ..conn("c1", "odoo-partner-1", "b1")
            })
            .unwrap());
        let c = s.connection_by_bunker("b1").unwrap().unwrap();
        assert_eq!(c.derive, derive);
        assert_eq!(s.bind_client("c1", "client").unwrap(), Bind::Bound);
        assert_eq!(
            s.bind_client("c1", "other").unwrap(),
            Bind::Already("client".into())
        );
        let c = s.connection("c1").unwrap().unwrap();
        assert_eq!(c.client_pubkey.as_deref(), Some("client"));
        assert_eq!(c.secret_hash, "", "the secret is spent");
        assert_eq!(s.connections("odoo-partner-1").unwrap().len(), 1);
        assert_eq!(s.live_bunker_pubkeys().unwrap(), vec!["b1".to_string()]);
        assert!(!s.revoke_connection("odoo-partner-2", "c1").unwrap());
        assert_eq!(
            s.revoke_connections("odoo-partner-1", Some("belfry-wallet"))
                .unwrap(),
            1
        );
        assert!(s.connection_by_bunker("b1").unwrap().is_none());
        assert!(s.connections("odoo-partner-1").unwrap().is_empty());
        assert!(s.live_bunker_pubkeys().unwrap().is_empty());
    }

    #[test]
    fn one_client_key_one_account_and_revoking_does_not_free_it() {
        let s = Store::in_memory().unwrap();
        for id in ["a", "b"] {
            s.put_account(id, id, Custody::Derived, 0, Some(b"k"))
                .unwrap();
        }
        let bound = |id, vine, bunker| NewConnection {
            client_pubkey: Some("client"),
            secret_hash: "",
            ..conn(id, vine, bunker)
        };
        assert!(s.add_connection(&bound("c1", "a", "b1")).unwrap());
        assert!(
            s.add_connection(&bound("c2", "a", "b2")).unwrap(),
            "the same account, again"
        );
        assert!(!s.add_connection(&bound("c3", "b", "b3")).unwrap());
        assert!(s.connection("c3").unwrap().is_none(), "nothing was written");
        // the other account's own connection cannot be bound to it by a connect either
        assert!(s.add_connection(&conn("c4", "b", "b4")).unwrap());
        assert_eq!(s.bind_client("c4", "client").unwrap(), Bind::Taken);
        assert_eq!(s.revoke_connections("a", None).unwrap(), 2);
        assert!(!s.add_connection(&bound("c5", "b", "b5")).unwrap());
        assert_eq!(s.bind_client("c4", "client").unwrap(), Bind::Taken);
        assert!(s.revoke_by_bunker("b", "b4").unwrap());
        assert!(!s.revoke_by_bunker("b", "b4").unwrap());
        assert_eq!(s.bind_client("c4", "free").unwrap(), Bind::Gone);
    }

    #[test]
    fn an_unclaimed_connection_ends_at_its_deadline_and_a_claimed_one_does_not() {
        let s = Store::in_memory().unwrap();
        s.put_account("a", "a", Custody::Derived, 0, Some(b"k"))
            .unwrap();
        let waiting = |id, bunker, at| NewConnection {
            expires_at: Some(at),
            ..conn(id, "a", bunker)
        };
        s.add_connection(&waiting("old", "b1", now() - 1)).unwrap();
        s.add_connection(&waiting("new", "b2", now() + 600))
            .unwrap();
        assert!(s.connection("old").unwrap().is_none());
        assert!(s.connection_by_bunker("b1").unwrap().is_none());
        assert_eq!(s.bind_client("old", "client").unwrap(), Bind::Gone);
        assert_eq!(s.live_bunker_pubkeys().unwrap(), vec!["b2".to_string()]);
        assert_eq!(s.bind_client("new", "client").unwrap(), Bind::Bound);
        s.set_expires_at("new", now() - 1);
        assert!(
            s.connection("new").unwrap().is_some(),
            "a claimed one stays"
        );
        assert_eq!(s.expire_unclaimed().unwrap(), 1);
        assert_eq!(s.connections("a").unwrap().len(), 1);
        s.touch_connection("new").unwrap();
        s.touch_connection("new").unwrap();
        assert_eq!(s.connection("new").unwrap().unwrap().uses, 2);
    }

    #[test]
    fn usernames_are_unique_whatever_the_case() {
        let s = Store::in_memory().unwrap();
        for id in ["a", "b"] {
            s.put_account(id, id, Custody::Derived, 0, Some(b"k"))
                .unwrap();
        }
        assert_eq!(s.set_username("a", "Lina").unwrap(), Named::Set);
        assert_eq!(s.set_username("a", "lina").unwrap(), Named::Set);
        assert_eq!(s.set_username("b", "LINA").unwrap(), Named::Taken);
        assert_eq!(s.set_username("c", "nobody").unwrap(), Named::NoAccount);
        assert_eq!(s.account_by_username("LiNa").unwrap().unwrap().vine_id, "a");
        assert_eq!(s.set_username("a", "lina-2").unwrap(), Named::Set);
        assert_eq!(
            s.set_username("b", "lina").unwrap(),
            Named::Set,
            "the old name is free"
        );
        // replacing the key keeps the name
        s.put_account("a", "zz", Custody::Own, 0, None).unwrap();
        assert_eq!(
            s.account("a").unwrap().unwrap().username.as_deref(),
            Some("lina-2")
        );
        assert_eq!(s.accounts_by_pubkey("zz").unwrap().len(), 1);
    }

    #[test]
    fn a_database_of_the_first_version_is_brought_up_to_date() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE accounts (
                vine_id TEXT PRIMARY KEY, pubkey TEXT NOT NULL, custody TEXT NOT NULL,
                key_version INTEGER NOT NULL DEFAULT 0, sealed BLOB,
                status TEXT NOT NULL DEFAULT 'active', created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL);
             CREATE TABLE connections (
                id TEXT PRIMARY KEY, vine_id TEXT NOT NULL REFERENCES accounts (vine_id),
                app TEXT NOT NULL, label TEXT NOT NULL, bunker_pubkey TEXT NOT NULL UNIQUE,
                bunker_sealed BLOB NOT NULL, secret TEXT NOT NULL, client_pubkey TEXT,
                derive TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                last_used_at INTEGER, revoked_at INTEGER);
             INSERT INTO accounts VALUES ('a', 'pk', 'derived', 0, x'00', 'active', 1, 1);
             INSERT INTO connections VALUES
                ('waiting', 'a', 'app', 'App', 'b1', x'00', 'plain-secret', NULL, '', 1, NULL, NULL),
                ('bound', 'a', 'app', 'App', 'b2', x'00', 'spent', 'client', '', 1, 5, NULL);",
        )
        .unwrap();
        let s = Store::init(conn).unwrap();
        let waiting = s.connection("waiting").unwrap().unwrap();
        assert_eq!(
            waiting.secret_hash,
            crate::signer::secret_hash("plain-secret")
        );
        assert_eq!(waiting.expires_at, None);
        let bound = s.connection("bound").unwrap().unwrap();
        assert_eq!(
            (bound.secret_hash.as_str(), bound.client_pubkey.as_deref()),
            ("", Some("client"))
        );
        assert!(!columns(&s.db(), "connections")
            .unwrap()
            .contains(&"secret".to_string()));
        assert_eq!(s.set_username("a", "lina").unwrap(), Named::Set);
    }
}
