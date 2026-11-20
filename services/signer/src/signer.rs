//! What the signer does, shared by the internal API and the NIP-46 bunker.

use std::path::PathBuf;

use nostr::nips::nip19::ToBech32;
use nostr::nips::{nip04, nip44};
use nostr::prelude::{Event, Timestamp};
use nostr::prelude::{Keys, PublicKey, SignEvent, UnsignedEvent};
use rand::RngCore;
use serde::Serialize;
use serde_json::Value;
use tokio::sync::Notify;
use zeroize::Zeroizing;

use crate::keys::{self, SeedError};
use crate::store::{Account, ConnectionRow, Custody, NewConnection, Store};
use crate::vault::Vault;

#[derive(Debug)]
pub enum Error {
    NotFound,
    SeedLocked,
    SeedMalformed,
    /// The member holds their own key; the signer cannot act for them.
    OwnKey,
    Suspended,
    /// The account's key is not the one the caller expected (restore of a pre-seed key).
    Mismatch(String),
    Invalid(String),
    Forbidden(String),
    Internal(String),
}

impl Error {
    pub fn code(&self) -> &'static str {
        match self {
            Error::NotFound => "not_found",
            Error::SeedLocked => "seed_locked",
            Error::SeedMalformed => "seed_malformed",
            Error::OwnKey => "own_key",
            Error::Suspended => "suspended",
            Error::Mismatch(_) => "mismatch",
            Error::Invalid(_) => "invalid",
            Error::Forbidden(_) => "forbidden",
            Error::Internal(_) => "internal",
        }
    }

    pub fn message(&self) -> String {
        match self {
            Error::NotFound => "No such account or connection.".into(),
            Error::SeedLocked => "The custody seed is locked; unlock it to create accounts.".into(),
            Error::SeedMalformed => "The custody seed file is not 64 hex characters.".into(),
            Error::OwnKey => "This member holds their own key.".into(),
            Error::Suspended => "This account is suspended.".into(),
            Error::Mismatch(m) | Error::Invalid(m) | Error::Forbidden(m) | Error::Internal(m) => {
                m.clone()
            }
        }
    }
}

impl From<rusqlite::Error> for Error {
    fn from(e: rusqlite::Error) -> Self {
        Error::Internal(format!("database: {e}"))
    }
}

impl From<SeedError> for Error {
    fn from(e: SeedError) -> Self {
        match e {
            SeedError::Locked => Error::SeedLocked,
            SeedError::Malformed => Error::SeedMalformed,
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;

pub struct Signer {
    pub store: Store,
    vault: Vault,
    seed_path: Option<PathBuf>,
    /// The bunker relay as clients reach it, written into bunker:// URLs.
    pub public_relays: Vec<String>,
    /// Woken when the set of live connections changed (the bunker resubscribes).
    pub connections_changed: Notify,
}

#[derive(Debug, Serialize)]
pub struct NewBunker {
    pub id: String,
    pub bunker_url: String,
    pub bunker_pubkey: String,
    pub pubkey: String,
}

pub enum Scheme {
    Nip44,
    Nip04,
}

impl Scheme {
    pub fn parse(s: &str) -> Result<Scheme> {
        match s {
            "nip44" => Ok(Scheme::Nip44),
            "nip04" => Ok(Scheme::Nip04),
            _ => Err(Error::Invalid(format!("unknown scheme {s:?}"))),
        }
    }
}

fn random_hex(bytes: usize) -> String {
    let mut buf = Zeroizing::new(vec![0u8; bytes]);
    rand::rngs::OsRng.fill_bytes(&mut buf);
    hex::encode(&*buf)
}

pub fn parse_pubkey(value: &str) -> Result<PublicKey> {
    PublicKey::parse(value).map_err(|_| Error::Invalid(format!("not a public key: {value:?}")))
}

fn valid_vine_id(vine_id: &str) -> Result<()> {
    let ok = !vine_id.is_empty()
        && vine_id.len() <= 64
        && vine_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if ok {
        Ok(())
    } else {
        Err(Error::Invalid(format!("invalid vine_id {vine_id:?}")))
    }
}

impl Signer {
    pub fn new(
        store: Store,
        vault: Vault,
        seed_path: Option<PathBuf>,
        public_relays: Vec<String>,
    ) -> Self {
        Self {
            store,
            vault,
            seed_path,
            public_relays,
            connections_changed: Notify::new(),
        }
    }

    pub fn seed_state(&self) -> &'static str {
        match keys::read_seed(self.seed_path.as_deref()) {
            Ok(_) => "unlocked",
            Err(SeedError::Locked) => "locked",
            Err(SeedError::Malformed) => "malformed",
        }
    }

    pub fn account(&self, vine_id: &str) -> Result<Account> {
        self.store.account(vine_id)?.ok_or(Error::NotFound)
    }

    /// Creates the account if it does not exist (idempotent on `vine_id`).
    ///
    /// - `own_pubkey`: the member holds this key; the signer keeps none. An existing account
    ///   moves to it (self-custody), which revokes its bunker connections.
    /// - otherwise the key is derived from the seed at `key_version`; with `expect_pubkey` the
    ///   derived key must be that one (restoring from Odoo), else nothing is written.
    pub fn ensure_account(
        &self,
        vine_id: &str,
        key_version: u32,
        own_pubkey: Option<&str>,
        expect_pubkey: Option<&str>,
    ) -> Result<Account> {
        valid_vine_id(vine_id)?;
        let existing = self.store.account(vine_id)?;
        if let Some(own) = own_pubkey {
            let own = parse_pubkey(own)?.to_hex();
            match existing {
                Some(a) if a.custody == Custody::Own && a.pubkey == own => return Ok(a),
                _ => return self.move_to_own_key(vine_id, &own),
            }
        }
        if let Some(a) = existing {
            return Ok(a);
        }
        let seed = keys::read_seed(self.seed_path.as_deref())?;
        let keys = keys::derive_keys(&seed, vine_id, key_version);
        let pubkey = keys.public_key().to_hex();
        if let Some(expected) = expect_pubkey {
            let expected = parse_pubkey(expected)?.to_hex();
            if expected != pubkey {
                return Err(Error::Mismatch(format!(
                    "the key derived for {vine_id} (version {key_version}) is not {expected}"
                )));
            }
        }
        let sealed = self.vault.seal(&keys);
        self.store.put_account(
            vine_id,
            &pubkey,
            Custody::Derived,
            key_version.into(),
            Some(&sealed),
        )?;
        tracing::info!(vine_id, pubkey = %pubkey, "account created (derived key)");
        self.account(vine_id)
    }

    /// Self-custody: the member's own key from now on. The held key is dropped and every
    /// bunker connection is revoked (apps sign in again with the member's own signer).
    pub fn move_to_own_key(&self, vine_id: &str, own_pubkey: &str) -> Result<Account> {
        valid_vine_id(vine_id)?;
        let own = parse_pubkey(own_pubkey)?.to_hex();
        let version = self
            .store
            .account(vine_id)?
            .map(|a| a.key_version)
            .unwrap_or(0);
        self.store
            .put_account(vine_id, &own, Custody::Own, version, None)?;
        let revoked = self.store.revoke_connections(vine_id, None)?;
        self.connections_changed.notify_one();
        tracing::info!(vine_id, pubkey = %own, revoked, "account moved to the member's own key");
        self.account(vine_id)
    }

    /// Imports a key sealed by Keycast under the same master key (migration). Accounts the
    /// signer already has are left as they are.
    pub fn import_sealed(
        &self,
        vine_id: &str,
        pubkey: &str,
        sealed: &[u8],
    ) -> Result<&'static str> {
        valid_vine_id(vine_id)?;
        if self.store.account(vine_id)?.is_some() {
            return Ok("exists");
        }
        let keys = self
            .vault
            .open(sealed)
            .map_err(|e| Error::Invalid(format!("{vine_id}: {e}")))?;
        let pubkey = parse_pubkey(pubkey)?.to_hex();
        if keys.public_key().to_hex() != pubkey {
            return Err(Error::Mismatch(format!(
                "{vine_id}: sealed key is not {pubkey}"
            )));
        }
        // A key the seed reproduces is a derived key; anything else must be kept sealed.
        // Versions above 8 do not occur: a version only rises when a key is replaced.
        let derived_version = keys::read_seed(self.seed_path.as_deref())
            .ok()
            .and_then(|seed| {
                (0..=8u32).find(|v| {
                    keys::derive_keys(&seed, vine_id, *v).public_key() == keys.public_key()
                })
            });
        let (custody, version) = match derived_version {
            Some(v) => (Custody::Derived, v),
            None => (Custody::Imported, 0),
        };
        self.store.put_account(
            vine_id,
            &pubkey,
            custody,
            version.into(),
            Some(&self.vault.seal(&keys)),
        )?;
        Ok(match custody {
            Custody::Derived => "derived",
            _ => "imported",
        })
    }

    pub fn set_status(&self, vine_id: &str, status: &str) -> Result<()> {
        if status != "active" && status != "suspended" {
            return Err(Error::Invalid(format!("unknown status {status:?}")));
        }
        if !self.store.set_status(vine_id, status)? {
            return Err(Error::NotFound);
        }
        self.connections_changed.notify_one();
        Ok(())
    }

    /// The key of an active account the signer holds.
    pub fn keys(&self, vine_id: &str) -> Result<Keys> {
        let account = self.account(vine_id)?;
        self.keys_of(&account)
    }

    /// The account's key for a request made through one of its connections (an app's access
    /// token): the connection must be live and the account's.
    pub fn keys_via(
        &self,
        vine_id: &str,
        connection: Option<&str>,
    ) -> Result<(Keys, Option<ConnectionRow>)> {
        let keys = self.keys(vine_id)?;
        let Some(id) = connection else {
            return Ok((keys, None));
        };
        match self.store.connection(id)? {
            Some(conn) if conn.vine_id == vine_id => {
                let _ = self.store.touch_connection(id);
                Ok((keys, Some(conn)))
            }
            _ => Err(Error::Forbidden("this connection has been revoked".into())),
        }
    }

    pub fn keys_of(&self, account: &Account) -> Result<Keys> {
        if account.status != "active" {
            return Err(Error::Suspended);
        }
        let sealed = account.sealed.as_deref().ok_or(Error::OwnKey)?;
        self.vault
            .open(sealed)
            .map_err(|e| Error::Internal(format!("{}: {e}", account.vine_id)))
    }

    /// Signs an event template (`kind`, `content`, `tags`, optional `created_at`). A `pubkey`
    /// in the template must be the account's.
    pub fn sign(&self, keys: &Keys, template: &Value) -> Result<Event> {
        let mut obj = template
            .as_object()
            .cloned()
            .ok_or_else(|| Error::Invalid("the event must be a JSON object".into()))?;
        let own = keys.public_key().to_hex();
        match obj.get("pubkey").and_then(Value::as_str) {
            Some(p) if !p.is_empty() && parse_pubkey(p)?.to_hex() != own => {
                return Err(Error::Invalid(
                    "the event's pubkey is not this account's".into(),
                ))
            }
            _ => {}
        }
        obj.insert("pubkey".into(), Value::String(own));
        obj.remove("id");
        obj.remove("sig");
        if !obj.get("created_at").is_some_and(Value::is_u64) {
            obj.insert("created_at".into(), Timestamp::now().as_secs().into());
        }
        if !obj.contains_key("tags") {
            obj.insert("tags".into(), Value::Array(vec![]));
        }
        if !obj.contains_key("content") {
            obj.insert("content".into(), Value::String(String::new()));
        }
        let unsigned = UnsignedEvent::from_json(Value::Object(obj).to_string())
            .map_err(|e| Error::Invalid(format!("not an event template: {e}")))?;
        keys.sign_event(unsigned)
            .map_err(|e| Error::Internal(format!("signing failed: {e}")))
    }

    pub fn encrypt(&self, keys: &Keys, scheme: Scheme, peer: &str, text: &str) -> Result<String> {
        let peer = parse_pubkey(peer)?;
        match scheme {
            Scheme::Nip44 => nip44::encrypt(keys.secret_key(), &peer, text, nip44::Version::V2)
                .map_err(|e| Error::Invalid(format!("nip44 encrypt: {e}"))),
            Scheme::Nip04 => nip04::encrypt(keys.secret_key(), &peer, text)
                .map_err(|e| Error::Invalid(format!("nip04 encrypt: {e}"))),
        }
    }

    pub fn decrypt(&self, keys: &Keys, scheme: Scheme, peer: &str, text: &str) -> Result<String> {
        let peer = parse_pubkey(peer)?;
        match scheme {
            Scheme::Nip44 => nip44::decrypt(keys.secret_key(), &peer, text)
                .map_err(|e| Error::Invalid(format!("nip44 decrypt: {e}"))),
            Scheme::Nip04 => nip04::decrypt(keys.secret_key(), &peer, text)
                .map_err(|e| Error::Invalid(format!("nip04 decrypt: {e}"))),
        }
    }

    pub fn derive(&self, keys: &Keys, purpose: &str) -> Result<String> {
        if purpose.is_empty() || purpose.len() > 64 {
            return Err(Error::Invalid("purpose must be 1 to 64 characters".into()));
        }
        Ok(hex::encode(*keys::derive_secret(keys, purpose)))
    }

    pub fn export(&self, vine_id: &str) -> Result<String> {
        let keys = self.keys(vine_id)?;
        let nsec = keys
            .secret_key()
            .to_bech32()
            .map_err(|e| Error::Internal(format!("bech32: {e}")))?;
        tracing::warn!(vine_id, "key exported");
        Ok(nsec)
    }

    // -- bunker connections ----------------------------------------------------------------

    /// A new bunker connection for an app. `keep`: revoke all but the newest `keep`
    /// connections of this app (Belfry's own apps sign in silently on every new device).
    pub fn create_connection(
        &self,
        vine_id: &str,
        app: &str,
        label: &str,
        derive: &[String],
        keep: Option<usize>,
    ) -> Result<NewBunker> {
        let account = self.account(vine_id)?;
        self.keys_of(&account)?;
        if app.is_empty() || app.len() > 64 || label.len() > 120 {
            return Err(Error::Invalid(
                "app must be 1 to 64 characters, label up to 120".into(),
            ));
        }
        let bunker = Keys::generate();
        let id = random_hex(16);
        let secret = random_hex(16);
        self.store.add_connection(&NewConnection {
            id: &id,
            vine_id,
            app,
            label,
            bunker_pubkey: &bunker.public_key().to_hex(),
            bunker_sealed: &self.vault.seal(&bunker),
            secret: &secret,
            derive,
        })?;
        if let Some(keep) = keep {
            self.store.prune_connections(vine_id, app, keep.max(1))?;
        }
        self.connections_changed.notify_one();
        let mut url = format!("bunker://{}?", bunker.public_key().to_hex());
        for relay in &self.public_relays {
            url.push_str(&format!("relay={}&", urlencode(relay)));
        }
        url.push_str(&format!("secret={secret}"));
        tracing::info!(vine_id, app, id = %id, "bunker connection created");
        Ok(NewBunker {
            id,
            bunker_url: url,
            bunker_pubkey: bunker.public_key().to_hex(),
            pubkey: account.pubkey,
        })
    }

    pub fn connections(&self, vine_id: &str) -> Result<Vec<ConnectionRow>> {
        self.account(vine_id)?;
        Ok(self.store.connections(vine_id)?)
    }

    pub fn revoke(&self, vine_id: &str, id: Option<&str>, app: Option<&str>) -> Result<usize> {
        let n = match id {
            Some(id) => usize::from(self.store.revoke_connection(vine_id, id)?),
            None => self.store.revoke_connections(vine_id, app)?,
        };
        if n > 0 {
            self.connections_changed.notify_one();
        }
        Ok(n)
    }

    pub fn bunker_keys(&self, conn: &ConnectionRow) -> Result<Keys> {
        self.vault
            .open(&conn.bunker_sealed)
            .map_err(|e| Error::Internal(format!("connection {}: {e}", conn.id)))
    }
}

pub fn urlencode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use base64::Engine;

    pub fn signer_with_seed(dir: &std::path::Path) -> Signer {
        let seed = dir.join("custody-seed");
        std::fs::write(&seed, "07".repeat(32)).unwrap();
        let vault =
            Vault::from_base64(&base64::engine::general_purpose::STANDARD.encode([3u8; 32]))
                .unwrap();
        Signer::new(
            Store::in_memory().unwrap(),
            vault,
            Some(seed),
            vec!["wss://bunker.example.org".into()],
        )
    }

    pub fn tempdir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("belfry-signer-{name}-{}", random_hex(4)));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn accounts_derive_sign_suspend_and_move_to_an_own_key() {
        let dir = tempdir("accounts");
        let s = signer_with_seed(&dir);
        let a = s.ensure_account("odoo-partner-42", 0, None, None).unwrap();
        assert_eq!(
            a.pubkey,
            keys::derive_keys(&[7u8; 32], "odoo-partner-42", 0)
                .public_key()
                .to_hex()
        );
        // idempotent, and the seed is not needed again
        std::fs::remove_file(dir.join("custody-seed")).unwrap();
        assert_eq!(
            s.ensure_account("odoo-partner-42", 0, None, None)
                .unwrap()
                .pubkey,
            a.pubkey
        );
        assert!(matches!(
            s.ensure_account("odoo-partner-43", 0, None, None),
            Err(Error::SeedLocked)
        ));

        let keys = s.keys("odoo-partner-42").unwrap();
        let ev = s
            .sign(
                &keys,
                &serde_json::json!({"kind": 1, "content": "hi", "tags": []}),
            )
            .unwrap();
        ev.verify().unwrap();
        assert_eq!(ev.pubkey.to_hex(), a.pubkey);
        let other = Keys::generate().public_key().to_hex();
        assert!(s
            .sign(
                &keys,
                &serde_json::json!({"kind": 1, "content": "", "pubkey": other})
            )
            .is_err());

        let peer = Keys::generate();
        let ct = s
            .encrypt(&keys, Scheme::Nip44, &peer.public_key().to_hex(), "secret")
            .unwrap();
        assert_eq!(
            nip44::decrypt(peer.secret_key(), &keys.public_key(), &ct).unwrap(),
            "secret"
        );
        assert_eq!(
            s.decrypt(&keys, Scheme::Nip44, &peer.public_key().to_hex(), &ct)
                .unwrap(),
            "secret"
        );

        s.set_status("odoo-partner-42", "suspended").unwrap();
        assert!(matches!(s.keys("odoo-partner-42"), Err(Error::Suspended)));
        s.set_status("odoo-partner-42", "active").unwrap();

        let c = s
            .create_connection("odoo-partner-42", "belfry-chat", "Chat", &[], None)
            .unwrap();
        assert!(c
            .bunker_url
            .starts_with(&format!("bunker://{}?relay=wss%3A%2F%2F", c.bunker_pubkey)));
        let own = Keys::generate().public_key().to_hex();
        let moved = s
            .ensure_account("odoo-partner-42", 0, Some(&own), None)
            .unwrap();
        assert_eq!(
            (moved.pubkey.as_str(), moved.custody),
            (own.as_str(), Custody::Own)
        );
        assert!(matches!(s.keys("odoo-partner-42"), Err(Error::OwnKey)));
        assert!(s.connections("odoo-partner-42").unwrap().is_empty());
    }

    #[test]
    fn restore_refuses_a_key_the_seed_does_not_give() {
        let dir = tempdir("restore");
        let s = signer_with_seed(&dir);
        let wrong = Keys::generate().public_key().to_hex();
        assert!(matches!(
            s.ensure_account("odoo-partner-7", 0, None, Some(&wrong)),
            Err(Error::Mismatch(_))
        ));
        assert!(s.store.account("odoo-partner-7").unwrap().is_none());
        assert!(s.ensure_account("bad id!", 0, None, None).is_err());
    }

    #[test]
    fn imports_keycast_keys_and_tells_derived_from_random_ones() {
        let dir = tempdir("import");
        let s = signer_with_seed(&dir);
        let derived = keys::derive_keys(&[7u8; 32], "odoo-partner-5", 1);
        let random = Keys::generate();
        let r1 = s
            .import_sealed(
                "odoo-partner-5",
                &derived.public_key().to_hex(),
                &s.vault.seal(&derived),
            )
            .unwrap();
        let r2 = s
            .import_sealed(
                "odoo-partner-6",
                &random.public_key().to_hex(),
                &s.vault.seal(&random),
            )
            .unwrap();
        assert_eq!((r1, r2), ("derived", "imported"));
        assert_eq!(s.account("odoo-partner-5").unwrap().key_version, 1);
        assert_eq!(
            s.import_sealed(
                "odoo-partner-6",
                &random.public_key().to_hex(),
                &s.vault.seal(&random)
            )
            .unwrap(),
            "exists"
        );
        assert!(s
            .import_sealed(
                "odoo-partner-8",
                &random.public_key().to_hex(),
                &s.vault.seal(&derived)
            )
            .is_err());
        assert_eq!(
            s.keys("odoo-partner-6").unwrap().public_key(),
            random.public_key()
        );
    }
}
