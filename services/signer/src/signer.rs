//! What the signer does, shared by the internal API and the NIP-46 bunker.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use nostr::nips::nip19::ToBech32;
use nostr::nips::{nip04, nip44};
use nostr::prelude::{Event, Timestamp};
use nostr::prelude::{Keys, PublicKey, SignEvent, UnsignedEvent};
use rand::RngCore;
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::sync::{watch, Notify};
use zeroize::Zeroizing;

use crate::keys::{self, SeedError};
use crate::store::{self, Account, Bind, ConnectionRow, Custody, Named, NewConnection, Store};
use crate::vault::Vault;

#[derive(Debug)]
pub enum Error {
    NotFound,
    SeedLocked,
    SeedMalformed,
    /// The member holds their own key; the signer cannot act for them.
    OwnKey,
    Suspended,
    /// The account's key is not the one the caller expected: nothing was created or changed.
    Mismatch(String),
    /// The client key is linked to another account (one client key, one identity).
    ClientKeyTaken,
    UsernameTaken,
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
            Error::ClientKeyTaken => "client_key_taken",
            Error::UsernameTaken => "username_taken",
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
            Error::ClientKeyTaken => "This client key is already linked to another account.".into(),
            Error::UsernameTaken => "This username is taken.".into(),
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

/// What the bunker loop tells the rest (`/health`, and an API call that waits for the relay
/// subscription to cover a connection it just made).
pub struct BunkerState {
    /// False without `BUNKER_RELAYS`.
    pub enabled: AtomicBool,
    pub connected: AtomicBool,
    /// When the last request arrived (seconds; 0: none yet).
    pub last_event_at: AtomicI64,
    pub restarts: AtomicU64,
    /// Bunker keys in the relay subscription (0 with `filtered` false: everything is read).
    pub keys: AtomicUsize,
    pub filtered: AtomicBool,
    /// Counts changes of the set of live connections…
    changes: AtomicU64,
    /// …and the last count the relay subscription covers.
    pub subscribed: watch::Sender<u64>,
}

impl BunkerState {
    fn new() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            connected: AtomicBool::new(false),
            last_event_at: AtomicI64::new(0),
            restarts: AtomicU64::new(0),
            keys: AtomicUsize::new(0),
            filtered: AtomicBool::new(false),
            changes: AtomicU64::new(0),
            subscribed: watch::channel(0).0,
        }
    }

    pub fn changes(&self) -> u64 {
        self.changes.load(Ordering::SeqCst)
    }
}

/// A token bucket per (connection, bound client or anyone else): the bunker's rate limit.
pub struct Limiter {
    buckets: Mutex<HashMap<(String, bool), (f64, Instant)>>,
    /// (burst, per second) for the connection's own client, and for every other sender together.
    pub client: (f64, f64),
    pub others: (f64, f64),
}

impl Limiter {
    fn new() -> Self {
        Self {
            buckets: Mutex::new(HashMap::new()),
            // a chat opening its history decrypts hundreds of messages in one go
            client: (600.0, 20.0),
            others: (20.0, 1.0),
        }
    }

    /// Takes one token; false when the bucket is empty.
    pub fn allow(&self, connection: &str, is_client: bool) -> bool {
        let (burst, rate) = if is_client { self.client } else { self.others };
        let now = Instant::now();
        let mut buckets = self.buckets.lock().unwrap_or_else(|e| e.into_inner());
        if buckets.len() > 20_000 {
            // full buckets say nothing: forget them
            buckets.retain(|_, (tokens, at)| {
                *tokens + now.duration_since(*at).as_secs_f64() * rate < burst
            });
        }
        let (tokens, at) = buckets
            .entry((connection.to_string(), is_client))
            .or_insert((burst, now));
        *tokens = (*tokens + now.duration_since(*at).as_secs_f64() * rate).min(burst);
        *at = now;
        if *tokens >= 1.0 {
            *tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

pub struct Signer {
    pub store: Store,
    vault: Vault,
    seed_path: Option<PathBuf>,
    /// The bunker relay as clients reach it, written into bunker:// URLs.
    pub public_relays: Vec<String>,
    /// Woken when the set of live connections changed (the bunker resubscribes).
    pub connections_changed: Notify,
    /// How long a connection made without a client key waits for its first `connect`.
    pub unclaimed_ttl: i64,
    pub bunker: BunkerState,
    pub limiter: Limiter,
}

#[derive(Debug, Serialize)]
pub struct NewBunker {
    pub id: String,
    pub bunker_url: String,
    pub bunker_pubkey: String,
    pub pubkey: String,
    pub app: String,
    pub created_at: i64,
    /// The client key the connection is bound to, when the request named one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub client_pubkey: Option<String>,
    /// For a connection waiting for its first `connect`: when it stops existing.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<i64>,
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

/// What the store keeps of a connection's secret: SHA-256, hex. The secret is 128 random
/// bits, so a plain hash is enough; it is compared in constant time (`bunker.rs`).
pub fn secret_hash(secret: &str) -> String {
    hex::encode(Sha256::digest(secret.as_bytes()))
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

/// A username as the signer stores it: lowercase, 1 to 63 characters of `a-z 0-9 . _ -`,
/// starting and ending with a letter or digit. The onboarding service's own rules are stricter
/// (`usernames.py`); this is the floor, the same for member numbers and `guest-<n>`.
pub fn valid_username(username: &str) -> Result<String> {
    let name = username.trim().to_ascii_lowercase();
    let edge = |c: Option<char>| c.is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit());
    let ok = !name.is_empty()
        && name.len() <= 63
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || "._-".contains(c))
        && edge(name.chars().next())
        && edge(name.chars().last());
    if ok {
        Ok(name)
    } else {
        Err(Error::Invalid(format!("invalid username {username:?}")))
    }
}

fn account_aad(vine_id: &str) -> String {
    format!("account:{vine_id}")
}

fn connection_aad(id: &str) -> String {
    format!("connection:{id}")
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
            unclaimed_ttl: 600,
            bunker: BunkerState::new(),
            limiter: Limiter::new(),
        }
    }

    pub fn seed_state(&self) -> &'static str {
        match keys::read_seed(self.seed_path.as_deref()) {
            Ok(_) => "unlocked",
            Err(SeedError::Locked) => "locked",
            Err(SeedError::Malformed) => "malformed",
        }
    }

    /// The set of live connections changed: the bunker subscribes again. Returns the change's
    /// number, for `wait_subscribed`.
    fn changed(&self) -> u64 {
        let n = self.bunker.changes.fetch_add(1, Ordering::SeqCst) + 1;
        self.connections_changed.notify_one();
        n
    }

    /// Waits (at most `timeout`) until the bunker's relay subscription covers change `n`, so a
    /// client that connects the moment it gets its bunker URL is heard. Returns at once when
    /// the bunker is off or not connected.
    pub async fn wait_subscribed(&self, n: u64, timeout: Duration) {
        if !self.bunker.enabled.load(Ordering::SeqCst)
            || !self.bunker.connected.load(Ordering::SeqCst)
        {
            return;
        }
        let mut rx = self.bunker.subscribed.subscribe();
        let _ = tokio::time::timeout(timeout, rx.wait_for(|seen| *seen >= n)).await;
    }

    /// One-time upgrade at start: keys still sealed in Keycast's format (no row binding; a
    /// database of the signer's first version) are sealed again, bound to their row, after
    /// checking that each holds the key its row names. Returns how many were upgraded; a blob
    /// that does not open or holds another key is an error (the signer does not start).
    pub fn upgrade_seals(&self) -> Result<usize> {
        let mut done = 0;
        for (vine_id, pubkey, sealed) in self.store.sealed_accounts()? {
            if !Vault::is_legacy(&sealed) {
                continue;
            }
            let keys = self
                .vault
                .open_legacy(&sealed)
                .map_err(|e| Error::Internal(format!("account {vine_id}: {e}")))?;
            if keys.public_key().to_hex() != pubkey {
                return Err(Error::Internal(format!(
                    "account {vine_id}: its sealed key is not {pubkey}"
                )));
            }
            self.store
                .set_sealed(&vine_id, &self.vault.seal(&keys, &account_aad(&vine_id)))?;
            done += 1;
        }
        for (id, pubkey, sealed) in self.store.sealed_connections()? {
            if !Vault::is_legacy(&sealed) {
                continue;
            }
            let keys = self
                .vault
                .open_legacy(&sealed)
                .map_err(|e| Error::Internal(format!("connection {id}: {e}")))?;
            if keys.public_key().to_hex() != pubkey {
                return Err(Error::Internal(format!(
                    "connection {id}: its sealed key is not {pubkey}"
                )));
            }
            self.store
                .set_bunker_sealed(&id, &self.vault.seal(&keys, &connection_aad(&id)))?;
            done += 1;
        }
        Ok(done)
    }

    pub fn account(&self, vine_id: &str) -> Result<Account> {
        self.store.account(vine_id)?.ok_or(Error::NotFound)
    }

    /// Creates the account if it does not exist (idempotent on `vine_id`), and never changes
    /// the key of one that does.
    ///
    /// - `own_pubkey`: the member holds this key; a new account gets it and no key of the
    ///   signer's. An existing account must already have that public key, else `mismatch`
    ///   (moving an account to the member's own key is `move_to_own_key`, on purpose a
    ///   separate call).
    /// - otherwise a new account's key is derived from the seed at `key_version`.
    /// - `expect_pubkey`: the public key the caller knows for this account. When the account
    ///   exists with another key, or the key that would be created is another, the answer is
    ///   `mismatch` and nothing is written: never a silent new key.
    /// - `username`: given to a new account (409 `username_taken`, nothing created, when
    ///   another account has it); an existing account keeps its own.
    pub fn ensure_account(
        &self,
        vine_id: &str,
        key_version: u32,
        own_pubkey: Option<&str>,
        expect_pubkey: Option<&str>,
        username: Option<&str>,
    ) -> Result<Account> {
        valid_vine_id(vine_id)?;
        let own = own_pubkey
            .map(|p| parse_pubkey(p).map(|k| k.to_hex()))
            .transpose()?;
        let expected = expect_pubkey
            .map(|p| parse_pubkey(p).map(|k| k.to_hex()))
            .transpose()?;
        let mismatch = |have: &str, want: &str| {
            Error::Mismatch(format!("the key of {vine_id} is {have}, not {want}"))
        };
        if let (Some(own), Some(expected)) = (&own, &expected) {
            if own != expected {
                return Err(mismatch(own, expected));
            }
        }
        if let Some(a) = self.store.account(vine_id)? {
            for want in [&own, &expected].into_iter().flatten() {
                if *want != a.pubkey {
                    return Err(mismatch(&a.pubkey, want));
                }
            }
            return Ok(a);
        }
        let username = username.map(valid_username).transpose()?;
        if let Some(name) = &username {
            if self.store.account_by_username(name)?.is_some() {
                return Err(Error::UsernameTaken);
            }
        }
        if let Some(own) = own {
            self.store
                .put_account(vine_id, &own, Custody::Own, key_version.into(), None)?;
            tracing::info!(vine_id, pubkey = %own, "account created (the member's own key)");
        } else {
            let seed = keys::read_seed(self.seed_path.as_deref())?;
            let keys = keys::derive_keys(&seed, vine_id, key_version);
            let pubkey = keys.public_key().to_hex();
            if let Some(expected) = expected {
                if expected != pubkey {
                    return Err(Error::Mismatch(format!(
                        "the key derived for {vine_id} (version {key_version}) is not {expected}"
                    )));
                }
            }
            let sealed = self.vault.seal(&keys, &account_aad(vine_id));
            self.store.put_account(
                vine_id,
                &pubkey,
                Custody::Derived,
                key_version.into(),
                Some(&sealed),
            )?;
            tracing::info!(vine_id, pubkey = %pubkey, "account created (derived key)");
        }
        if let Some(name) = &username {
            // lost a race for the name: the account exists without it, and the caller hears 409
            if self.store.set_username(vine_id, name)? != Named::Set {
                return Err(Error::UsernameTaken);
            }
        }
        self.account(vine_id)
    }

    /// Self-custody: the member's own key from now on. The held key is dropped and every
    /// bunker connection is revoked (apps sign in again with the member's own signer). The
    /// key the signer holds for the account is refused: Belfry can recompute it, so keeping it
    /// is not self-custody.
    pub fn move_to_own_key(&self, vine_id: &str, own_pubkey: &str) -> Result<Account> {
        valid_vine_id(vine_id)?;
        let own = parse_pubkey(own_pubkey)?.to_hex();
        let existing = self.account(vine_id)?;
        if existing.pubkey == own {
            if existing.custody == Custody::Own {
                return Ok(existing);
            }
            return Err(Error::Invalid(
                "this is the key Belfry holds for the account: move to a new key".into(),
            ));
        }
        self.store
            .put_account(vine_id, &own, Custody::Own, existing.key_version, None)?;
        let revoked = self.store.revoke_connections(vine_id, None)?;
        self.changed();
        tracing::info!(vine_id, pubkey = %own, revoked, "account moved to the member's own key");
        self.account(vine_id)
    }

    /// Imports a key sealed by Keycast under the same master key (migration). An account the
    /// signer already has with that public key is left as it is (`exists`); with another
    /// public key it is an error (`mismatch`), never an overwrite.
    pub fn import_sealed(
        &self,
        vine_id: &str,
        pubkey: &str,
        sealed: &[u8],
    ) -> Result<&'static str> {
        valid_vine_id(vine_id)?;
        let pubkey = parse_pubkey(pubkey)?.to_hex();
        if let Some(existing) = self.store.account(vine_id)? {
            if existing.pubkey == pubkey {
                return Ok("exists");
            }
            return Err(Error::Mismatch(format!(
                "{vine_id}: the signer has this account with the key {}, not {pubkey}",
                existing.pubkey
            )));
        }
        let keys = self
            .vault
            .open_legacy(sealed)
            .map_err(|e| Error::Invalid(format!("{vine_id}: {e}")))?;
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
            Some(&self.vault.seal(&keys, &account_aad(vine_id))),
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
        Ok(())
    }

    /// The same for the account(s) with this public key; how many were set.
    pub fn set_status_by_pubkey(&self, pubkey: &str, status: &str) -> Result<usize> {
        let pubkey = parse_pubkey(pubkey)?.to_hex();
        let accounts = self.store.accounts_by_pubkey(&pubkey)?;
        if accounts.is_empty() {
            return Err(Error::NotFound);
        }
        for a in &accounts {
            self.set_status(&a.vine_id, status)?;
        }
        Ok(accounts.len())
    }

    /// The account's username: its NIP-05 name. 409 when another account has it.
    pub fn set_username(&self, vine_id: &str, username: &str) -> Result<Account> {
        let name = valid_username(username)?;
        match self.store.set_username(vine_id, &name)? {
            Named::Set => self.account(vine_id),
            Named::Taken => Err(Error::UsernameTaken),
            Named::NoAccount => Err(Error::NotFound),
        }
    }

    /// The account with this username (any case).
    pub fn by_username(&self, username: &str) -> Result<Account> {
        let name = valid_username(username).map_err(|_| Error::NotFound)?;
        self.store
            .account_by_username(&name)?
            .ok_or(Error::NotFound)
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

    /// The key sealed on the account's row: it must open as this row's (the vine_id is the
    /// seal's associated data) and be the key the row names.
    pub fn keys_of(&self, account: &Account) -> Result<Keys> {
        if account.status != "active" {
            return Err(Error::Suspended);
        }
        let sealed = account.sealed.as_deref().ok_or(Error::OwnKey)?;
        self.vault
            .open_for(sealed, &account_aad(&account.vine_id), &account.pubkey)
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

    /// A new bunker connection for an app.
    ///
    /// - `client_pubkey`: the NIP-46 client key the connection is for. It is bound to that key
    ///   from the start: no other key can use it, the URL's secret opens nothing (none is
    ///   kept) and the connection does not expire. Refused (`client_key_taken`, nothing
    ///   created) when the key is linked to another account.
    /// - without it: the URL's secret binds the first client key that sends `connect` with it,
    ///   within `unclaimed_ttl` seconds; after that the connection no longer exists.
    /// - `keep`: revoke all but the newest `keep` connections of this app.
    ///
    /// Returns the bunker and the number of the change (`wait_subscribed`).
    pub fn create_connection(
        &self,
        vine_id: &str,
        app: &str,
        label: &str,
        derive: &[String],
        keep: Option<usize>,
        client_pubkey: Option<&str>,
    ) -> Result<(NewBunker, u64)> {
        let account = self.account(vine_id)?;
        self.keys_of(&account)?;
        if app.is_empty() || app.chars().count() > 64 || label.chars().count() > 120 {
            return Err(Error::Invalid(
                "app must be 1 to 64 characters, label up to 120".into(),
            ));
        }
        let client = client_pubkey
            .map(|p| parse_pubkey(p).map(|k| k.to_hex()))
            .transpose()?;
        let bunker = Keys::generate();
        let id = random_hex(16);
        let secret = random_hex(16);
        let expires_at = match client {
            Some(_) => None,
            None => Some(store::now() + self.unclaimed_ttl),
        };
        let added = self.store.add_connection(&NewConnection {
            id: &id,
            vine_id,
            app,
            label,
            bunker_pubkey: &bunker.public_key().to_hex(),
            bunker_sealed: &self.vault.seal(&bunker, &connection_aad(&id)),
            secret_hash: &match client {
                Some(_) => String::new(),
                None => secret_hash(&secret),
            },
            client_pubkey: client.as_deref(),
            derive,
            expires_at,
        })?;
        if !added {
            tracing::warn!(
                vine_id,
                app,
                "bunker connection refused: the client key is linked to another account"
            );
            return Err(Error::ClientKeyTaken);
        }
        if let Some(keep) = keep {
            self.store.prune_connections(vine_id, app, keep.max(1))?;
        }
        let change = self.changed();
        let mut url = format!("bunker://{}?", bunker.public_key().to_hex());
        for relay in &self.public_relays {
            url.push_str(&format!("relay={}&", urlencode(relay)));
        }
        url.push_str(&format!("secret={secret}"));
        tracing::info!(vine_id, app, id = %id, bound = client.is_some(), "bunker connection created");
        Ok((
            NewBunker {
                id,
                bunker_url: url,
                bunker_pubkey: bunker.public_key().to_hex(),
                pubkey: account.pubkey,
                app: app.to_string(),
                created_at: store::now(),
                client_pubkey: client,
                expires_at,
            },
            change,
        ))
    }

    /// Binds a connection to the client that presented its secret in `connect`.
    pub fn bind_client(&self, conn: &ConnectionRow, client_pubkey: &str) -> Result<Bind> {
        Ok(self.store.bind_client(&conn.id, client_pubkey)?)
    }

    pub fn connections(&self, vine_id: &str) -> Result<Vec<ConnectionRow>> {
        self.account(vine_id)?;
        Ok(self.store.connections(vine_id)?)
    }

    /// Revokes one connection (by `id` or by its bunker key), an app's, or all of them.
    pub fn revoke(
        &self,
        vine_id: &str,
        id: Option<&str>,
        bunker_pubkey: Option<&str>,
        app: Option<&str>,
    ) -> Result<usize> {
        let n = match (id, bunker_pubkey) {
            (Some(id), _) => usize::from(self.store.revoke_connection(vine_id, id)?),
            (None, Some(bunker)) => {
                let bunker = parse_pubkey(bunker)?.to_hex();
                usize::from(self.store.revoke_by_bunker(vine_id, &bunker)?)
            }
            (None, None) => self.store.revoke_connections(vine_id, app)?,
        };
        if n > 0 {
            self.changed();
        }
        Ok(n)
    }

    /// Housekeeping, from the bunker loop: unclaimed connections past their deadline.
    pub fn expire_unclaimed(&self) -> usize {
        let n = self.store.expire_unclaimed().unwrap_or(0);
        if n > 0 {
            tracing::info!(n, "unclaimed bunker connections expired");
            self.changed();
        }
        n
    }

    pub fn bunker_keys(&self, conn: &ConnectionRow) -> Result<Keys> {
        self.vault
            .open_for(
                &conn.bunker_sealed,
                &connection_aad(&conn.id),
                &conn.bunker_pubkey,
            )
            .map_err(|e| Error::Internal(format!("connection {}: {e}", conn.id)))
    }

    #[cfg(test)]
    pub fn seal_legacy_for_tests(&self, keys: &Keys) -> Vec<u8> {
        // Keycast's format: the bound one without its first byte and with no associated data
        // cannot be made from `seal`, so the test vault writes it itself.
        self.vault.seal_legacy(keys)
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

    fn pk() -> String {
        Keys::generate().public_key().to_hex()
    }

    #[test]
    fn accounts_derive_sign_suspend_and_move_to_an_own_key() {
        let dir = tempdir("accounts");
        let s = signer_with_seed(&dir);
        let a = s
            .ensure_account("odoo-partner-42", 0, None, None, None)
            .unwrap();
        assert_eq!(
            a.pubkey,
            keys::derive_keys(&[7u8; 32], "odoo-partner-42", 0)
                .public_key()
                .to_hex()
        );
        // idempotent, and the seed is not needed again
        std::fs::remove_file(dir.join("custody-seed")).unwrap();
        assert_eq!(
            s.ensure_account("odoo-partner-42", 0, None, None, None)
                .unwrap()
                .pubkey,
            a.pubkey
        );
        assert!(matches!(
            s.ensure_account("odoo-partner-43", 0, None, None, None),
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
        let other = pk();
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
        assert_eq!(s.set_status_by_pubkey(&a.pubkey, "active").unwrap(), 1);
        assert!(matches!(
            s.set_status_by_pubkey(&pk(), "active"),
            Err(Error::NotFound)
        ));

        let (c, _) = s
            .create_connection("odoo-partner-42", "belfry-chat", "Chat", &[], None, None)
            .unwrap();
        assert!(c
            .bunker_url
            .starts_with(&format!("bunker://{}?relay=wss%3A%2F%2F", c.bunker_pubkey)));
        // keeping the key Belfry can recompute is not self-custody
        assert!(matches!(
            s.move_to_own_key("odoo-partner-42", &a.pubkey),
            Err(Error::Invalid(_))
        ));
        let own = pk();
        let moved = s.move_to_own_key("odoo-partner-42", &own).unwrap();
        assert_eq!(
            (moved.pubkey.as_str(), moved.custody),
            (own.as_str(), Custody::Own)
        );
        assert!(matches!(s.keys("odoo-partner-42"), Err(Error::OwnKey)));
        assert!(s.connections("odoo-partner-42").unwrap().is_empty());
        // again: nothing changes; an unknown account cannot be moved
        assert_eq!(
            s.move_to_own_key("odoo-partner-42", &own).unwrap().pubkey,
            own
        );
        assert!(matches!(
            s.move_to_own_key("odoo-partner-99", &own),
            Err(Error::NotFound)
        ));
    }

    #[test]
    fn restore_refuses_a_key_the_seed_does_not_give() {
        let dir = tempdir("restore");
        let s = signer_with_seed(&dir);
        let wrong = pk();
        assert!(matches!(
            s.ensure_account("odoo-partner-7", 0, None, Some(&wrong), None),
            Err(Error::Mismatch(_))
        ));
        assert!(s.store.account("odoo-partner-7").unwrap().is_none());
        assert!(s.ensure_account("bad id!", 0, None, None, None).is_err());
    }

    #[test]
    fn an_expected_key_is_checked_against_the_account_that_exists() {
        let dir = tempdir("expect");
        let s = signer_with_seed(&dir);
        let a = s.ensure_account("guest-3", 0, None, None, None).unwrap();
        // the right key: fine, whichever way it is said
        s.ensure_account("guest-3", 0, None, Some(&a.pubkey), None)
            .unwrap();
        // another key: an error, and the account keeps its key and its connections
        let (c, _) = s
            .create_connection("guest-3", "chat", "Chat", &[], None, None)
            .unwrap();
        for (own, expect) in [(None, Some(pk())), (Some(pk()), None)] {
            assert!(matches!(
                s.ensure_account("guest-3", 0, own.as_deref(), expect.as_deref(), None),
                Err(Error::Mismatch(_))
            ));
        }
        // a higher version does not make a new key either
        assert_eq!(
            s.ensure_account("guest-3", 5, None, None, None)
                .unwrap()
                .pubkey,
            a.pubkey
        );
        assert_eq!(s.account("guest-3").unwrap().custody, Custody::Derived);
        assert!(s.store.connection(&c.id).unwrap().is_some());

        // an account with the member's own key: created once, then checked the same way
        let own = pk();
        let o = s
            .ensure_account("odoo-partner-8", 0, Some(&own), None, None)
            .unwrap();
        assert_eq!((o.custody, o.pubkey.as_str()), (Custody::Own, own.as_str()));
        s.ensure_account("odoo-partner-8", 0, Some(&own), Some(&own), None)
            .unwrap();
        assert!(matches!(
            s.ensure_account("odoo-partner-8", 0, Some(&pk()), None, None),
            Err(Error::Mismatch(_))
        ));
        assert!(matches!(
            s.ensure_account("odoo-partner-9", 0, Some(&own), Some(&pk()), None),
            Err(Error::Mismatch(_))
        ));
        assert!(s.store.account("odoo-partner-9").unwrap().is_none());
    }

    #[test]
    fn usernames_are_chosen_unique_and_found_again() {
        let dir = tempdir("names");
        let s = signer_with_seed(&dir);
        let a = s
            .ensure_account("odoo-partner-1", 0, None, None, Some("0001"))
            .unwrap();
        assert_eq!(a.username.as_deref(), Some("0001"));
        // a taken name creates nothing
        assert!(matches!(
            s.ensure_account("odoo-partner-2", 0, None, None, Some("0001")),
            Err(Error::UsernameTaken)
        ));
        assert!(s.store.account("odoo-partner-2").unwrap().is_none());
        s.ensure_account("odoo-partner-2", 0, None, None, Some("guest-2"))
            .unwrap();
        // an existing account keeps the name it has
        let again = s
            .ensure_account("odoo-partner-1", 0, None, None, Some("other"))
            .unwrap();
        assert_eq!(again.username.as_deref(), Some("0001"));

        assert_eq!(
            s.set_username("odoo-partner-1", "Lina")
                .unwrap()
                .username
                .as_deref(),
            Some("lina")
        );
        assert!(matches!(
            s.set_username("odoo-partner-2", "LINA"),
            Err(Error::UsernameTaken)
        ));
        assert!(matches!(
            s.set_username("odoo-partner-3", "nobody"),
            Err(Error::NotFound)
        ));
        for bad in ["", "-lina", "lina-", "li na", "lina@x", &"a".repeat(64)] {
            assert!(
                matches!(
                    s.set_username("odoo-partner-2", bad),
                    Err(Error::Invalid(_))
                ),
                "{bad:?}"
            );
        }
        assert_eq!(s.by_username("LINA").unwrap().pubkey, a.pubkey);
        assert!(matches!(s.by_username("0001"), Err(Error::NotFound)));
        assert!(matches!(s.by_username("not a name"), Err(Error::NotFound)));
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
                &s.seal_legacy_for_tests(&derived),
            )
            .unwrap();
        let r2 = s
            .import_sealed(
                "guest-6",
                &random.public_key().to_hex(),
                &s.seal_legacy_for_tests(&random),
            )
            .unwrap();
        assert_eq!((r1, r2), ("derived", "imported"));
        assert_eq!(s.account("odoo-partner-5").unwrap().key_version, 1);
        assert_eq!(
            s.import_sealed(
                "guest-6",
                &random.public_key().to_hex(),
                &s.seal_legacy_for_tests(&random)
            )
            .unwrap(),
            "exists"
        );
        // the same account with another key: an error, and the account is untouched
        let other = Keys::generate();
        assert!(matches!(
            s.import_sealed(
                "guest-6",
                &other.public_key().to_hex(),
                &s.seal_legacy_for_tests(&other)
            ),
            Err(Error::Mismatch(_))
        ));
        assert!(s
            .import_sealed(
                "odoo-partner-8",
                &random.public_key().to_hex(),
                &s.seal_legacy_for_tests(&derived)
            )
            .is_err());
        assert_eq!(s.keys("guest-6").unwrap().public_key(), random.public_key());
    }

    #[test]
    fn a_sealed_key_moved_to_another_row_is_refused() {
        let dir = tempdir("rows");
        let s = signer_with_seed(&dir);
        let a = s
            .ensure_account("odoo-partner-1", 0, None, None, None)
            .unwrap();
        let b = s
            .ensure_account("odoo-partner-2", 0, None, None, None)
            .unwrap();
        // b's row gets a's sealed key: neither as b's key nor under b's name does it open
        s.store
            .set_sealed("odoo-partner-2", a.sealed.as_deref().unwrap())
            .unwrap();
        assert!(matches!(s.keys("odoo-partner-2"), Err(Error::Internal(_))));
        // a blob that opens for the row but holds another key than the row names
        let stranger = Keys::generate();
        s.store
            .set_sealed(
                "odoo-partner-2",
                &s.vault.seal(&stranger, &account_aad("odoo-partner-2")),
            )
            .unwrap();
        assert!(matches!(s.keys("odoo-partner-2"), Err(Error::Internal(_))));
        s.store
            .set_sealed("odoo-partner-2", b.sealed.as_deref().unwrap())
            .unwrap();
        assert_eq!(
            s.keys("odoo-partner-2").unwrap().public_key().to_hex(),
            b.pubkey
        );

        // the same for a connection's bunker key
        let (c1, _) = s
            .create_connection("odoo-partner-1", "chat", "Chat", &[], None, None)
            .unwrap();
        let (c2, _) = s
            .create_connection("odoo-partner-1", "chat", "Chat", &[], None, None)
            .unwrap();
        let row1 = s.store.connection(&c1.id).unwrap().unwrap();
        s.store
            .set_bunker_sealed(&c2.id, &row1.bunker_sealed)
            .unwrap();
        let row2 = s.store.connection(&c2.id).unwrap().unwrap();
        assert!(s.bunker_keys(&row2).is_err());
        assert!(s.bunker_keys(&row1).is_ok());
    }

    #[test]
    fn keys_sealed_the_old_way_are_bound_to_their_rows_at_start() {
        let dir = tempdir("upgrade");
        let s = signer_with_seed(&dir);
        let keys = keys::derive_keys(&[7u8; 32], "odoo-partner-1", 0);
        let pubkey = keys.public_key().to_hex();
        s.store
            .put_account(
                "odoo-partner-1",
                &pubkey,
                Custody::Derived,
                0,
                Some(&s.seal_legacy_for_tests(&keys)),
            )
            .unwrap();
        // not on the signing path before the upgrade
        assert!(matches!(s.keys("odoo-partner-1"), Err(Error::Internal(_))));
        assert_eq!(s.upgrade_seals().unwrap(), 1);
        assert_eq!(s.upgrade_seals().unwrap(), 0);
        assert_eq!(
            s.keys("odoo-partner-1").unwrap().public_key().to_hex(),
            pubkey
        );
        // a legacy blob holding another key than its row names stops the upgrade
        let stranger = Keys::generate();
        s.store
            .put_account(
                "odoo-partner-2",
                &pk(),
                Custody::Imported,
                0,
                Some(&s.seal_legacy_for_tests(&stranger)),
            )
            .unwrap();
        assert!(s.upgrade_seals().is_err());
    }

    #[test]
    fn a_connection_made_for_a_client_key_is_bound_and_one_key_is_one_account() {
        let dir = tempdir("bound");
        let s = signer_with_seed(&dir);
        s.ensure_account("odoo-partner-1", 0, None, None, None)
            .unwrap();
        s.ensure_account("guest-2", 0, None, None, None).unwrap();
        let client = pk();
        let (c, n) = s
            .create_connection(
                "odoo-partner-1",
                "Chat · Firefox on Linux",
                "",
                &[],
                None,
                Some(&client.to_uppercase()),
            )
            .unwrap();
        assert_eq!(c.client_pubkey.as_deref(), Some(client.as_str()));
        assert_eq!((c.expires_at, n), (None, s.bunker.changes()));
        let row = s.store.connection(&c.id).unwrap().unwrap();
        assert_eq!(
            (row.secret_hash.as_str(), row.client_pubkey.as_deref()),
            ("", Some(client.as_str())),
            "no secret is kept: the URL alone opens nothing"
        );
        assert!(matches!(
            s.create_connection("guest-2", "Chat", "", &[], None, Some(&client)),
            Err(Error::ClientKeyTaken)
        ));
        assert!(s.connections("guest-2").unwrap().is_empty());
        // revoking does not free the key; the same account may use it again
        assert_eq!(
            s.revoke("odoo-partner-1", None, Some(&c.bunker_pubkey), None)
                .unwrap(),
            1
        );
        assert!(matches!(
            s.create_connection("guest-2", "Chat", "", &[], None, Some(&client)),
            Err(Error::ClientKeyTaken)
        ));
        s.create_connection("odoo-partner-1", "Chat", "", &[], None, Some(&client))
            .unwrap();
        assert!(matches!(
            s.create_connection("guest-2", "Chat", "", &[], None, Some("nonsense")),
            Err(Error::Invalid(_))
        ));

        // without a client key: the secret is kept as a hash only, with a deadline
        let (u, _) = s
            .create_connection("guest-2", "Notes", "", &[], None, None)
            .unwrap();
        let secret = u.bunker_url.split("secret=").nth(1).unwrap();
        let row = s.store.connection(&u.id).unwrap().unwrap();
        assert_eq!(row.secret_hash, secret_hash(secret));
        assert_ne!(row.secret_hash, secret);
        assert_eq!(u.expires_at, row.expires_at);
        assert!(row.expires_at.unwrap() > store::now() + 500);
        s.store.set_expires_at(&u.id, store::now() - 1);
        assert_eq!(s.expire_unclaimed(), 1);
        assert!(s.connections("guest-2").unwrap().is_empty());
    }

    #[test]
    fn the_bunkers_rate_limit_is_per_connection_and_spares_the_client_from_strangers() {
        let mut l = Limiter::new();
        l.client = (3.0, 0.0);
        l.others = (1.0, 0.0);
        assert!(l.allow("c1", false));
        assert!(!l.allow("c1", false), "strangers share a small bucket");
        assert!(l.allow("c1", true) && l.allow("c1", true) && l.allow("c1", true));
        assert!(!l.allow("c1", true));
        assert!(l.allow("c2", true), "another connection is not affected");
    }
}
