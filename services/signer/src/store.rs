//! The signer's own data, in one SQLite file: accounts (who has which key, sealed) and bunker
//! connections. Odoo stays the source of truth for passes and key versions; everything here can
//! be rebuilt from the custody seed and Odoo (`python -m belfry_onboarding restore-keys`),
//! except keys imported from Keycast that predate the seed, which the backups carry.

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
    #[serde(skip)]
    pub sealed: Option<Vec<u8>>,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct ConnectionRow {
    pub id: String,
    pub vine_id: String,
    pub app: String,
    pub label: String,
    pub bunker_pubkey: String,
    #[serde(skip)]
    pub bunker_sealed: Vec<u8>,
    #[serde(skip)]
    pub secret: String,
    pub client_pubkey: Option<String>,
    pub derive: Vec<String>,
    pub created_at: i64,
    pub last_used_at: Option<i64>,
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
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS accounts_pubkey ON accounts (pubkey);
CREATE TABLE IF NOT EXISTS connections (
    id            TEXT PRIMARY KEY,
    vine_id       TEXT NOT NULL REFERENCES accounts (vine_id),
    app           TEXT NOT NULL,
    label         TEXT NOT NULL,
    bunker_pubkey TEXT NOT NULL UNIQUE,
    bunker_sealed BLOB NOT NULL,
    secret        TEXT NOT NULL,
    client_pubkey TEXT,
    derive        TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER,
    revoked_at    INTEGER
);
CREATE INDEX IF NOT EXISTS connections_vine_id ON connections (vine_id);
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
    })
}

const ACCOUNT_COLS: &str =
    "vine_id, pubkey, custody, key_version, sealed, status, created_at, updated_at";
const CONNECTION_COLS: &str = "id, vine_id, app, label, bunker_pubkey, bunker_sealed, secret, \
     client_pubkey, derive, created_at, last_used_at";

fn connection_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ConnectionRow> {
    let derive: String = row.get(8)?;
    Ok(ConnectionRow {
        id: row.get(0)?,
        vine_id: row.get(1)?,
        app: row.get(2)?,
        label: row.get(3)?,
        bunker_pubkey: row.get(4)?,
        bunker_sealed: row.get(5)?,
        secret: row.get(6)?,
        client_pubkey: row.get(7)?,
        derive: derive
            .split(',')
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect(),
        created_at: row.get(9)?,
        last_used_at: row.get(10)?,
    })
}

pub struct NewConnection<'a> {
    pub id: &'a str,
    pub vine_id: &'a str,
    pub app: &'a str,
    pub label: &'a str,
    pub bunker_pubkey: &'a str,
    pub bunker_sealed: &'a [u8],
    pub secret: &'a str,
    pub derive: &'a [String],
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
        Ok(Self {
            db: Mutex::new(conn),
        })
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

    pub fn count_accounts(&self) -> rusqlite::Result<i64> {
        self.db()
            .query_row("SELECT count(*) FROM accounts", [], |r| r.get(0))
    }

    /// Inserts the account, or replaces it (the key and how it is held; the status is kept).
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

    pub fn set_status(&self, vine_id: &str, status: &str) -> rusqlite::Result<bool> {
        let n = self.db().execute(
            "UPDATE accounts SET status = ?2, updated_at = ?3 WHERE vine_id = ?1",
            params![vine_id, status, now()],
        )?;
        Ok(n > 0)
    }

    // -- connections -----------------------------------------------------------------------

    pub fn add_connection(&self, c: &NewConnection<'_>) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO connections (id, vine_id, app, label, bunker_pubkey, bunker_sealed,
                                      secret, derive, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                c.id,
                c.vine_id,
                c.app,
                c.label,
                c.bunker_pubkey,
                c.bunker_sealed,
                c.secret,
                c.derive.join(","),
                now()
            ],
        )?;
        Ok(())
    }

    pub fn connections(&self, vine_id: &str) -> rusqlite::Result<Vec<ConnectionRow>> {
        let db = self.db();
        let mut stmt = db.prepare(&format!(
            "SELECT {CONNECTION_COLS} FROM connections
             WHERE vine_id = ?1 AND revoked_at IS NULL ORDER BY created_at DESC, id"
        ))?;
        let rows = stmt.query_map(params![vine_id], connection_from_row)?;
        rows.collect()
    }

    pub fn connection(&self, id: &str) -> rusqlite::Result<Option<ConnectionRow>> {
        self.db()
            .query_row(
                &format!(
                    "SELECT {CONNECTION_COLS} FROM connections
                     WHERE id = ?1 AND revoked_at IS NULL"
                ),
                params![id],
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
                     WHERE bunker_pubkey = ?1 AND revoked_at IS NULL"
                ),
                params![bunker_pubkey],
                connection_from_row,
            )
            .optional()
    }

    /// Binds the connection to the first client key that presents its secret.
    pub fn bind_client(&self, id: &str, client_pubkey: &str) -> rusqlite::Result<()> {
        self.db().execute(
            "UPDATE connections SET client_pubkey = ?2, last_used_at = ?3
             WHERE id = ?1 AND client_pubkey IS NULL",
            params![id, client_pubkey, now()],
        )?;
        Ok(())
    }

    pub fn touch_connection(&self, id: &str) -> rusqlite::Result<()> {
        self.db().execute(
            "UPDATE connections SET last_used_at = ?2 WHERE id = ?1",
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
}

#[cfg(test)]
mod tests {
    use super::*;

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
        s.add_connection(&NewConnection {
            id: "c1",
            vine_id: "odoo-partner-1",
            app: "belfry-wallet",
            label: "Wallet",
            bunker_pubkey: "b1",
            bunker_sealed: b"x",
            secret: "s",
            derive: &derive,
        })
        .unwrap();
        let c = s.connection_by_bunker("b1").unwrap().unwrap();
        assert_eq!(c.derive, derive);
        s.bind_client("c1", "client").unwrap();
        s.bind_client("c1", "other").unwrap();
        assert_eq!(
            s.connection("c1")
                .unwrap()
                .unwrap()
                .client_pubkey
                .as_deref(),
            Some("client")
        );
        assert_eq!(s.connections("odoo-partner-1").unwrap().len(), 1);
        assert!(!s.revoke_connection("odoo-partner-2", "c1").unwrap());
        assert_eq!(
            s.revoke_connections("odoo-partner-1", Some("belfry-wallet"))
                .unwrap(),
            1
        );
        assert!(s.connection_by_bunker("b1").unwrap().is_none());
        assert!(s.connections("odoo-partner-1").unwrap().is_empty());
    }
}
