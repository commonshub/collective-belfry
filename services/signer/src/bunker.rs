//! The NIP-46 bunker: answers remote-signing requests on the bunker relay.
//!
//! Every connection has a key of its own (the `bunker://<key>` in its URL), so one connection
//! can be revoked without touching the others. A connection belongs to one client key: the
//! one it was made for, or the first that presents the URL's secret in `connect`. Requests are
//! NIP-44 (or NIP-04, for older clients) encrypted kind-24133 events p-tagged to the
//! connection's key; the answer goes back the same way. Methods: connect, get_public_key,
//! sign_event, nip04/nip44 encrypt and decrypt, ping, get_relays, switch_relays, and Belfry's
//! derive_secret for the purposes the connection was given. A connection of an outside app
//! (scope `outside`) is refused `sign_event` for the events `limits.rs` names, and decrypts only
//! with the member's consent (the connection's `decrypt` flag); everything else is the same
//! for both scopes.
//!
//! A request is taken once (by its event id), only when its signature is right and its
//! `created_at` is within ten minutes of now (`FRESH`); which rate limit it counts against is
//! decided after the signature is checked, so an event that only names a client's key spends
//! nothing of that client's allowance.
//!
//! The loop (`run`) is supervised: when the relay client's notification stream ends or the
//! loop panics it starts again with a growing pause and says so in the log. It asks the relay
//! only for requests to the bunker keys of live connections (`#p`), again whenever the set
//! changes; a subscription that fails is tried again on the next tick, then with a growing
//! pause, and the bunker says "not listening" meanwhile (`/health`, and the API does not hand
//! out a new connection). Requests are answered concurrently, each connection within its rate
//! limit.

use std::collections::{HashSet, VecDeque};
use std::future::Future;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use nostr::nips::{nip04, nip44};
use nostr::prelude::{
    Event, EventBuilder, EventId, Filter, FinalizeEvent, Keys, Kind, PublicKey, SubscriptionId,
    Tag, Timestamp,
};
use nostr_sdk::prelude::{Client, ClientNotification, StreamExt};
use serde_json::{json, Value};
use subtle::ConstantTimeEq;
use tokio::sync::Semaphore;

use crate::limits;
use crate::signer::{secret_hash, Error, Scheme, Signer};
use crate::store::{self, Bind, ConnectionRow};

/// Requests answered at the same time.
const CONCURRENCY: usize = 16;
/// Bunker keys per filter, and the most keys asked for by name: with more live connections
/// the bunker reads every kind-24133 event again and drops the ones that are not for it.
const KEYS_PER_FILTER: usize = 500;
const MAX_KEYS: usize = 5000;
/// On every (re)subscription the relay is asked for the last minute too: a request sent while
/// the bunker was reconnecting is answered if the relay kept it (most relays do not keep
/// kind 24133; then this changes nothing).
const LOOK_BACK: u64 = 60;
/// The subscription is renewed this often, so its look-back never reaches far.
const RENEW: Duration = Duration::from_secs(300);
/// A request is taken when its `created_at` is at most this far from the signer's clock, either
/// way (seconds). Wider than the look-back, so everything a renewed subscription brings back is
/// still fresh; a client whose clock is further off gets no answer.
pub const FRESH: i64 = 600;
/// How long a request's id is remembered: longer than it could be replayed (an event stamped
/// `FRESH` ahead stays fresh for twice `FRESH`).
pub const SEEN_FOR: i64 = 2 * FRESH + 60;
/// The most ids remembered (32 bytes each, twice); beyond it the oldest go first.
pub const SEEN_KEEP: usize = 200_000;

/// The largest request the bunker decrypts (NIP-44 allows far more; nothing Belfry signs is
/// near this).
const MAX_CONTENT: usize = 256 * 1024;

#[derive(Clone, Copy)]
enum Wire {
    Nip44,
    Nip04,
}

fn open(bunker: &Keys, sender: &PublicKey, content: &str) -> Option<(Wire, String)> {
    if content.len() > MAX_CONTENT {
        return None;
    }
    if let Ok(plain) = nip44::decrypt(bunker.secret_key(), sender, content) {
        return Some((Wire::Nip44, plain));
    }
    nip04::decrypt(bunker.secret_key(), sender, content)
        .ok()
        .map(|plain| (Wire::Nip04, plain))
}

fn seal(bunker: &Keys, to: &PublicKey, wire: Wire, plain: &str) -> Option<String> {
    match wire {
        Wire::Nip44 => nip44::encrypt(bunker.secret_key(), to, plain, nip44::Version::V2).ok(),
        Wire::Nip04 => nip04::encrypt(bunker.secret_key(), to, plain).ok(),
    }
}

fn param(params: &[Value], i: usize) -> Result<String, String> {
    params
        .get(i)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| format!("missing parameter {i}"))
}

fn err_text(e: Error) -> String {
    match e {
        Error::Suspended => "unauthorized: the account is suspended".into(),
        Error::OwnKey => "unauthorized: the member holds their own key".into(),
        other => other.message(),
    }
}

/// Answers one request of `sender` on `conn`; `Ok` is the result string of the response.
pub fn handle(
    signer: &Signer,
    conn: &ConnectionRow,
    sender: &PublicKey,
    method: &str,
    params: &[Value],
) -> Result<String, String> {
    let sender_hex = sender.to_hex();
    if method == "connect" {
        const OTHER: &str = "unauthorized: this connection belongs to another client";
        return match &conn.client_pubkey {
            // the client it belongs to: apps send connect again whenever they start; a
            // secret is no longer looked at (none is kept)
            Some(bound) if *bound == sender_hex => {
                let _ = signer.store.touch_connection(&conn.id);
                Ok("ack".into())
            }
            Some(_) => Err(OTHER.into()),
            None => {
                // an HTTP sign-in nobody connected to in time: its secret has ended
                if !conn.listening(store::now()) {
                    return Err("unauthorized: this connection has ended".into());
                }
                let secret = params.get(1).and_then(Value::as_str).unwrap_or("");
                let given = secret_hash(secret);
                let right = !secret.is_empty()
                    && !conn.secret_hash.is_empty()
                    && bool::from(given.as_bytes().ct_eq(conn.secret_hash.as_bytes()));
                if !right {
                    return Err("unauthorized: wrong or missing secret".into());
                }
                match signer
                    .bind_client(conn, &sender_hex)
                    .map_err(|e| e.message())?
                {
                    Bind::Bound => {
                        tracing::info!(connection = %conn.id, app = %conn.app, "bunker client connected");
                        Ok("ack".into())
                    }
                    Bind::Already(bound) if bound == sender_hex => Ok("ack".into()),
                    Bind::Already(_) => Err(OTHER.into()),
                    Bind::Taken => {
                        tracing::warn!(connection = %conn.id, "connect refused: the client key is linked to another account");
                        Err("unauthorized: Client key is linked to another account".into())
                    }
                    Bind::Gone => Err("unauthorized: this connection has ended".into()),
                }
            }
        };
    }
    if conn.client_pubkey.as_deref() != Some(sender_hex.as_str()) {
        return Err("unauthorized: connect first".into());
    }
    let _ = signer.store.touch_connection(&conn.id);
    if method == "ping" {
        return Ok("pong".into());
    }
    let account = signer.account(&conn.vine_id).map_err(err_text)?;
    let keys = signer.keys_of(&account).map_err(err_text)?;
    match method {
        "get_public_key" => Ok(keys.public_key().to_hex()),
        "get_relays" => Ok("{}".into()),
        // NIP-46: "null" when the client should stay on the relays it has
        "switch_relays" => Ok("null".into()),
        "sign_event" => {
            let template: Value = serde_json::from_str(&param(params, 0)?)
                .map_err(|_| "sign_event: the event is not JSON".to_string())?;
            if conn.scope == store::SCOPE_OUTSIDE && limits::forbidden(&template, &signer.own) {
                let kind = template.get("kind").cloned().unwrap_or_default();
                tracing::warn!(
                    connection = %conn.id,
                    app = %conn.app,
                    kind = %kind,
                    "sign_event refused: not for an outside app"
                );
                return Err(limits::REFUSAL.into());
            }
            let event = signer.sign(&keys, &template).map_err(err_text)?;
            Ok(event.as_json())
        }
        "nip44_encrypt" | "nip04_encrypt" | "nip44_decrypt" | "nip04_decrypt" => {
            let scheme = if method.starts_with("nip44") {
                Scheme::Nip44
            } else {
                Scheme::Nip04
            };
            // reading what was written to the member: an outside app needs their word for it
            if conn.scope == store::SCOPE_OUTSIDE && limits::method_refused(method, conn.decrypt) {
                tracing::warn!(
                    connection = %conn.id,
                    app = %conn.app,
                    method,
                    "decrypt refused: no consent for this outside app"
                );
                return Err(limits::REFUSAL.into());
            }
            let (peer, text) = (param(params, 0)?, param(params, 1)?);
            if method.ends_with("encrypt") {
                signer
                    .encrypt(&keys, scheme, &peer, &text)
                    .map_err(err_text)
            } else {
                signer
                    .decrypt(&keys, scheme, &peer, &text)
                    .map_err(err_text)
            }
        }
        "derive_secret" => {
            let purpose = param(params, 0)?;
            if !conn.derive.contains(&purpose) {
                tracing::warn!(connection = %conn.id, app = %conn.app, purpose, "derive_secret refused");
                return Err(format!("unauthorized: this app may not derive {purpose:?}"));
            }
            signer.derive(&keys, &purpose).map_err(err_text)
        }
        other => Err(format!("unsupported method {other:?}")),
    }
}

/// Turns one kind-24133 event into the response event, or None when it is not for a
/// connection the bunker answers on, not signed by who it says, not from now, a repeat, over
/// the connection's rate limit, or cannot be read. A request for a bunker key the signer does
/// not have is dropped on the store lookup alone: before its signature is checked and before
/// anything is decrypted. The order matters: the signature first, then freshness and the
/// repeat check, and only then the rate limit of the sender the signature proved.
pub fn respond(signer: &Signer, event: &Event) -> Option<Event> {
    if event.kind != Kind::NostrConnect {
        return None;
    }
    let bunker_hex = event.tags.public_keys().next()?.to_hex();
    let conn = signer.store.connection_by_bunker(&bunker_hex).ok()??;
    let now = store::now();
    if !conn.listening(now) {
        return None;
    }
    event.verify().ok()?;
    let stamped = i64::try_from(event.created_at.as_secs()).unwrap_or(i64::MAX);
    if stamped.saturating_sub(now).abs() > FRESH {
        tracing::debug!(connection = %conn.id, stamped, "bunker request not from now: dropped");
        return None;
    }
    if !signer.seen.first(event.id, now) {
        return None;
    }
    let is_client = conn.client_pubkey.as_deref() == Some(event.pubkey.to_hex().as_str());
    if !signer.limiter.allow(&conn.id, is_client) {
        tracing::debug!(connection = %conn.id, is_client, "bunker request over the rate limit: dropped");
        return None;
    }
    let bunker = match signer.bunker_keys(&conn) {
        Ok(keys) => keys,
        Err(e) => {
            tracing::error!("{}", e.message());
            return None;
        }
    };
    let (wire, plain) = open(&bunker, &event.pubkey, &event.content)?;
    let request: Value = serde_json::from_str(&plain).ok()?;
    let id = request.get("id").and_then(Value::as_str)?.to_string();
    let method = request.get("method").and_then(Value::as_str).unwrap_or("");
    let params = request
        .get("params")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let body = match handle(signer, &conn, &event.pubkey, method, &params) {
        Ok(result) => json!({"id": id, "result": result}),
        Err(error) => {
            tracing::info!(connection = %conn.id, method, error = %error, "bunker request refused");
            json!({"id": id, "result": "", "error": error})
        }
    };
    let content = seal(&bunker, &event.pubkey, wire, &body.to_string())?;
    EventBuilder::new(Kind::NostrConnect, content)
        .tag(Tag::public_key(event.pubkey))
        .finalize(&bunker)
        .ok()
}

/// The ids, and the order they came in with the second each was seen.
type SeenIds = (HashSet<EventId>, VecDeque<(EventId, i64)>);

/// The requests already taken in, by event id: a request is answered once, however often it
/// arrives (after a reconnect, a renewed subscription, from two relays, or replayed by
/// somebody who recorded it). An id is kept for `ttl` seconds, which is longer than the event
/// stays fresh, so a replay is either remembered or too old; `keep` bounds the memory.
pub struct Seen {
    inner: Mutex<SeenIds>,
    keep: usize,
    ttl: i64,
}

impl Seen {
    pub fn new(keep: usize, ttl: i64) -> Self {
        Self {
            inner: Mutex::new((HashSet::new(), VecDeque::new())),
            keep,
            ttl,
        }
    }

    /// True the first time `id` is seen (`now`: seconds).
    pub fn first(&self, id: EventId, now: i64) -> bool {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        while inner.1.front().is_some_and(|(_, at)| at + self.ttl <= now) {
            if let Some((old, _)) = inner.1.pop_front() {
                inner.0.remove(&old);
            }
        }
        if !inner.0.insert(id) {
            return false;
        }
        inner.1.push_back((id, now));
        while inner.1.len() > self.keep {
            if let Some((old, _)) = inner.1.pop_front() {
                inner.0.remove(&old);
            }
        }
        true
    }
}

/// When the relay subscription is made again: every `RENEW` while it stands; after a failure
/// on the next tick, then after 5, 10, 20, 40 and 60 seconds. While it does not stand the
/// bunker is not listening for new connections, and says so.
struct Renewal {
    subscribed: bool,
    failures: u32,
    next_try: Instant,
    renew_at: Instant,
}

impl Renewal {
    fn new(now: Instant) -> Self {
        Self {
            subscribed: false,
            failures: 0,
            next_try: now,
            renew_at: now + RENEW,
        }
    }

    fn pause(failures: u32) -> Duration {
        match failures {
            0 | 1 => Duration::ZERO,
            n => Duration::from_secs((5u64 << (n - 2).min(4)).min(60)),
        }
    }

    /// What a round came to.
    fn record(&mut self, took: bool, now: Instant) {
        if took {
            self.subscribed = true;
            self.failures = 0;
            self.renew_at = now + RENEW;
        } else {
            // the subscription there was may still stand at the relay, but it does not cover
            // the connections of today: not subscribed
            self.subscribed = false;
            self.failures += 1;
            self.next_try = now + Self::pause(self.failures);
        }
    }

    /// Whether a tick should try now.
    fn due(&self, now: Instant, connected: bool) -> bool {
        connected
            && if self.subscribed {
                now >= self.renew_at
            } else {
                now >= self.next_try
            }
    }
}

/// The relay subscription for these bunker keys: by `#p`, a few hundred keys a filter; a
/// filter nobody matches when there is no connection; everything when there are too many.
/// The second value says whether the relay filters by key.
pub fn filters_for(keys: &[String], nobody: &PublicKey, since: Timestamp) -> (Vec<Filter>, bool) {
    let base = Filter::new().kind(Kind::NostrConnect).since(since);
    if keys.len() > MAX_KEYS {
        return (vec![base], false);
    }
    let parsed: Vec<PublicKey> = keys
        .iter()
        .filter_map(|k| PublicKey::from_hex(k).ok())
        .collect();
    if parsed.is_empty() {
        return (vec![base.pubkey(*nobody)], true);
    }
    (
        parsed
            .chunks(KEYS_PER_FILTER)
            .map(|chunk| base.clone().pubkeys(chunk.iter().copied()))
            .collect(),
        true,
    )
}

/// Runs `session` again and again: when it returns (the reason is logged) or panics, after a
/// pause that doubles from 1 s to 60 s and starts over once a session has lasted a minute.
pub async fn supervise<F, Fut>(signer: Arc<Signer>, mut session: F)
where
    F: FnMut() -> Fut,
    Fut: Future<Output = &'static str> + Send + 'static,
{
    let mut pause = Duration::from_secs(1);
    loop {
        let started = Instant::now();
        let why = match tokio::spawn(session()).await {
            Ok(reason) => reason.to_string(),
            Err(e) if e.is_panic() => "it panicked".to_string(),
            Err(e) => format!("it was stopped: {e}"),
        };
        signer.bunker.connected.store(false, Ordering::SeqCst);
        let restarts = signer.bunker.restarts.fetch_add(1, Ordering::SeqCst) + 1;
        if started.elapsed() >= Duration::from_secs(60) {
            pause = Duration::from_secs(1);
        }
        tracing::error!(
            why = %why,
            restarts,
            retry_in_seconds = pause.as_secs(),
            "the bunker loop ended; starting it again"
        );
        tokio::time::sleep(pause).await;
        pause = (pause * 2).min(Duration::from_secs(60));
    }
}

/// Runs the bunker until the process ends.
pub async fn run(signer: Arc<Signer>, relays: Vec<String>) {
    if relays.is_empty() {
        tracing::warn!("no BUNKER_RELAYS: the NIP-46 bunker is off");
        return;
    }
    signer.bunker.enabled.store(true, Ordering::SeqCst);
    let s = signer.clone();
    supervise(signer, move || session(s.clone(), relays.clone())).await;
}

/// The bunker's relay subscription. It is replaced, never edited (the relay client refuses a
/// second subscription under one id): the new one is made first, then the old one is closed,
/// so no request falls between the two; an event that arrives on both is answered once.
struct Subscription {
    current: Option<SubscriptionId>,
    round: u64,
    nobody: PublicKey,
}

impl Subscription {
    fn new() -> Self {
        Self {
            current: None,
            round: 0,
            nobody: Keys::generate().public_key(),
        }
    }

    /// Subscribes to the live connections' bunker keys; true when a relay took it.
    async fn renew(&mut self, signer: &Signer, client: &Client) -> bool {
        // the count first: a connection made while this runs is covered by the next round
        let covers = signer.bunker.changes();
        let keys = match signer.store.live_bunker_pubkeys() {
            Ok(keys) => keys,
            Err(e) => {
                tracing::error!("bunker: connections not read: {e}");
                return false;
            }
        };
        let since = Timestamp::from_secs((store::now() as u64).saturating_sub(LOOK_BACK));
        let (filters, filtered) = filters_for(&keys, &self.nobody, since);
        self.round += 1;
        let id = SubscriptionId::new(format!("belfry-bunker-{}", self.round));
        match client.subscribe(filters).with_id(id.clone()).await {
            Ok(out) if !out.success.is_empty() => {
                if let Some(old) = self.current.replace(id) {
                    if let Err(e) = client.unsubscribe(&old).await {
                        tracing::warn!("bunker: old subscription not closed: {e}");
                    }
                }
                signer.bunker.keys.store(keys.len(), Ordering::SeqCst);
                signer.bunker.filtered.store(filtered, Ordering::SeqCst);
                signer.bunker.connected.store(true, Ordering::SeqCst);
                signer.bunker.subscribed.send_replace(covers);
                true
            }
            Ok(out) => {
                tracing::warn!(failed = ?out.failed, "bunker subscription: no relay took it; trying again");
                false
            }
            Err(e) => {
                tracing::warn!("bunker subscription failed ({e}); trying again");
                false
            }
        }
    }
}

async fn any_connected(client: &Client) -> bool {
    client
        .relays()
        .await
        .values()
        .any(|relay| relay.status().is_connected())
}

/// One life of the bunker: a relay client, its subscription, and the requests it answers.
/// Returns why it ended.
async fn session(signer: Arc<Signer>, relays: Vec<String>) -> &'static str {
    let client = Client::default();
    for relay in &relays {
        if let Err(e) = client.add_relay(relay.as_str()).await {
            tracing::error!(relay, "bunker relay: {e}");
        }
    }
    let mut notifications = client.notifications();
    client.connect().and_wait(Duration::from_secs(5)).await;
    let mut subscription = Subscription::new();
    let mut renewal = Renewal::new(Instant::now());
    let took = subscription.renew(&signer, &client).await;
    renewal.record(took, Instant::now());
    if took {
        tracing::info!(
            ?relays,
            keys = signer.bunker.keys.load(Ordering::SeqCst),
            "bunker listening"
        );
    }
    let limit = Arc::new(Semaphore::new(CONCURRENCY));
    let mut tick = tokio::time::interval(Duration::from_secs(5));
    loop {
        tokio::select! {
            notification = notifications.next() => {
                let event = match notification {
                    None => return "the relay client's notification stream ended",
                    Some(ClientNotification::Shutdown) => return "the relay client shut down",
                    Some(ClientNotification::Event { event, .. }) => event,
                    Some(_) => continue,
                };
                // a repeat is told in `respond`, once the signature is checked
                if event.kind != Kind::NostrConnect {
                    continue;
                }
                signer.bunker.last_event_at.store(store::now(), Ordering::SeqCst);
                // at most CONCURRENCY at once: the rest wait here, in the order they came
                let Ok(permit) = limit.clone().acquire_owned().await else {
                    return "the request limit was closed";
                };
                let (signer, client) = (signer.clone(), client.clone());
                tokio::spawn(async move {
                    let _permit = permit;
                    match tokio::task::spawn_blocking(move || respond(&signer, &event)).await {
                        Ok(Some(answer)) => {
                            if let Err(e) = client.send_event(&answer).await {
                                tracing::warn!("bunker answer not sent: {e}");
                            }
                        }
                        Ok(None) => {}
                        // one request's handler: the loop goes on
                        Err(e) => tracing::error!("a bunker request handler failed: {e}"),
                    }
                });
            }
            _ = signer.connections_changed.notified() => {
                // a change is asked for at once, whatever the pause: somebody waits for it
                let took = subscription.renew(&signer, &client).await;
                renewal.record(took, Instant::now());
                if !took {
                    signer.bunker.connected.store(false, Ordering::SeqCst);
                }
            }
            _ = tick.tick() => {
                signer.expire_unclaimed();
                let connected = any_connected(&client).await;
                if renewal.due(Instant::now(), connected) {
                    let was = renewal.subscribed;
                    let took = subscription.renew(&signer, &client).await;
                    renewal.record(took, Instant::now());
                    if took && !was {
                        tracing::info!(?relays, "bunker listening");
                    }
                }
                signer
                    .bunker
                    .connected
                    .store(connected && renewal.subscribed, Ordering::SeqCst);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::signer::tests::{signer_with_seed, tempdir};

    fn request(bunker_pk: &PublicKey, client: &Keys, body: Value) -> Event {
        let content = nip44::encrypt(
            client.secret_key(),
            bunker_pk,
            body.to_string(),
            nip44::Version::V2,
        )
        .unwrap();
        EventBuilder::new(Kind::NostrConnect, content)
            .tag(Tag::public_key(*bunker_pk))
            .finalize(client)
            .unwrap()
    }

    fn answer(signer: &Signer, bunker_pk: &PublicKey, client: &Keys, body: Value) -> Value {
        let ev = respond(signer, &request(bunker_pk, client, body)).expect("an answer");
        assert_eq!(ev.pubkey, *bunker_pk);
        assert_eq!(ev.tags.public_keys().next(), Some(client.public_key()));
        serde_json::from_str(&nip44::decrypt(client.secret_key(), bunker_pk, &ev.content).unwrap())
            .unwrap()
    }

    #[test]
    fn a_client_connects_with_the_secret_and_then_signs() {
        let dir = tempdir("bunker");
        let s = signer_with_seed(&dir);
        let account = s
            .ensure_account("odoo-partner-42", 0, None, None, None)
            .unwrap();
        let (c, _) = s
            .create_connection(
                "odoo-partner-42",
                "belfry-wallet",
                "Wallet",
                &["cashu-wallet-v1".into()],
                None,
                None,
            )
            .unwrap();
        let bunker_pk = PublicKey::from_hex(&c.bunker_pubkey).unwrap();
        let secret = c.bunker_url.split("secret=").nth(1).unwrap().to_string();
        let client = Keys::generate();
        let intruder = Keys::generate();

        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "1", "method": "get_public_key", "params": []}),
        );
        assert_eq!(r["error"], "unauthorized: connect first");
        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "2", "method": "connect", "params": [c.bunker_pubkey, "wrong"]}),
        );
        assert!(r["error"].as_str().unwrap().starts_with("unauthorized"));
        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "3", "method": "connect", "params": [c.bunker_pubkey, secret]}),
        );
        assert_eq!(
            (r["id"].as_str(), r["result"].as_str()),
            (Some("3"), Some("ack"))
        );
        // the secret is spent: another client cannot take the connection over
        let r = answer(
            &s,
            &bunker_pk,
            &intruder,
            json!({"id": "4", "method": "connect", "params": [c.bunker_pubkey, secret]}),
        );
        assert!(r["error"].as_str().unwrap().starts_with("unauthorized"));

        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "5", "method": "get_public_key", "params": []}),
        );
        assert_eq!(r["result"], account.pubkey);
        let template = json!({"kind": 1, "content": "hello", "tags": [], "created_at": 1700000000});
        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "6", "method": "sign_event", "params": [template.to_string()]}),
        );
        let ev = Event::from_json(r["result"].as_str().unwrap()).unwrap();
        ev.verify().unwrap();
        assert_eq!(ev.pubkey.to_hex(), account.pubkey);
        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "7", "method": "derive_secret", "params": ["cashu-wallet-v1"]}),
        );
        assert_eq!(r["result"].as_str().unwrap().len(), 64);
        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "8", "method": "derive_secret", "params": ["other"]}),
        );
        assert!(r["error"].as_str().unwrap().starts_with("unauthorized"));
        let peer = Keys::generate();
        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "9", "method": "nip44_encrypt", "params": [peer.public_key().to_hex(), "psst"]}),
        );
        let pk = PublicKey::from_hex(&account.pubkey).unwrap();
        assert_eq!(
            nip44::decrypt(peer.secret_key(), &pk, r["result"].as_str().unwrap()).unwrap(),
            "psst"
        );

        s.set_status("odoo-partner-42", "suspended").unwrap();
        let r = answer(
            &s,
            &bunker_pk,
            &client,
            json!({"id": "10", "method": "sign_event", "params": [template.to_string()]}),
        );
        assert!(r["error"].as_str().unwrap().contains("suspended"));
        s.set_status("odoo-partner-42", "active").unwrap();

        s.revoke("odoo-partner-42", Some(&c.id), None, None)
            .unwrap();
        assert!(respond(
            &s,
            &request(
                &bunker_pk,
                &client,
                json!({"id": "11", "method": "ping", "params": []})
            )
        )
        .is_none());
    }

    #[test]
    fn nip04_requests_get_nip04_answers() {
        let dir = tempdir("bunker04");
        let s = signer_with_seed(&dir);
        s.ensure_account("odoo-partner-1", 0, None, None, None)
            .unwrap();
        let (c, _) = s
            .create_connection("odoo-partner-1", "outside", "Amethyst", &[], None, None)
            .unwrap();
        let bunker_pk = PublicKey::from_hex(&c.bunker_pubkey).unwrap();
        let secret = c.bunker_url.split("secret=").nth(1).unwrap();
        let client = Keys::generate();
        let body = json!({"id": "a", "method": "connect", "params": [c.bunker_pubkey, secret]});
        let content = nip04::encrypt(client.secret_key(), &bunker_pk, body.to_string()).unwrap();
        let ev = EventBuilder::new(Kind::NostrConnect, content)
            .tag(Tag::public_key(bunker_pk))
            .finalize(&client)
            .unwrap();
        let out = respond(&s, &ev).unwrap();
        let plain = nip04::decrypt(client.secret_key(), &bunker_pk, &out.content).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&plain).unwrap()["result"],
            "ack"
        );
    }

    fn two_accounts(name: &str) -> Signer {
        let s = signer_with_seed(&tempdir(name));
        s.ensure_account("odoo-partner-1", 0, None, None, None)
            .unwrap();
        s.ensure_account("guest-2", 0, None, None, None).unwrap();
        s
    }

    fn ask(s: &Signer, bunker: &str, client: &Keys, method: &str, params: Value) -> Value {
        answer(
            s,
            &PublicKey::from_hex(bunker).unwrap(),
            client,
            json!({"id": "x", "method": method, "params": params}),
        )
    }

    #[test]
    fn a_connection_made_for_a_client_needs_no_secret_and_takes_no_other_client() {
        let s = two_accounts("bound");
        let client = Keys::generate();
        let (c, _) = s
            .create_connection(
                "odoo-partner-1",
                "Chat",
                "",
                &[],
                None,
                Some(&client.public_key().to_hex()),
            )
            .unwrap();
        let secret = c.bunker_url.split("secret=").nth(1).unwrap().to_string();
        // the client just starts sending requests…
        let r = ask(&s, &c.bunker_pubkey, &client, "get_public_key", json!([]));
        assert_eq!(r["result"], c.pubkey);
        assert!(r.get("error").is_none(), "no error key on success: {r}");
        // …or connects as usual, with the secret, without it, and again after a restart
        for params in [
            json!([c.bunker_pubkey, secret]),
            json!([c.bunker_pubkey]),
            json!([]),
        ] {
            let r = ask(&s, &c.bunker_pubkey, &client, "connect", params);
            assert_eq!(
                (r["id"].as_str(), r["result"].as_str()),
                (Some("x"), Some("ack"))
            );
        }
        assert_eq!(
            ask(&s, &c.bunker_pubkey, &client, "ping", json!([]))["result"],
            "pong"
        );
        assert_eq!(
            ask(&s, &c.bunker_pubkey, &client, "switch_relays", json!([]))["result"],
            "null"
        );
        // someone who saw the URL: the secret opens nothing
        let thief = Keys::generate();
        let r = ask(
            &s,
            &c.bunker_pubkey,
            &thief,
            "connect",
            json!([c.bunker_pubkey, secret]),
        );
        assert!(r["error"].as_str().unwrap().starts_with("unauthorized"));
        assert_eq!(r["result"], "");
        let r = ask(&s, &c.bunker_pubkey, &thief, "sign_event", json!(["{}"]));
        assert_eq!(r["error"], "unauthorized: connect first");
        let row = s.store.connection(&c.id).unwrap().unwrap();
        assert_eq!(row.client_pubkey, Some(client.public_key().to_hex()));
        assert!(row.uses >= 5 && row.last_used_at.is_some());
    }

    #[test]
    fn one_client_key_one_identity_on_the_first_connect_too() {
        let s = two_accounts("identity");
        let client = Keys::generate();
        let hex = client.public_key().to_hex();
        // the key belongs to the member's account (a connection made for it, since revoked)
        let (mine, _) = s
            .create_connection("odoo-partner-1", "Chat", "", &[], None, Some(&hex))
            .unwrap();
        s.revoke("odoo-partner-1", Some(&mine.id), None, None)
            .unwrap();
        // another account's connection, with the right secret: refused, and still unclaimed
        let (theirs, _) = s
            .create_connection("guest-2", "Notes", "", &[], None, None)
            .unwrap();
        let secret = theirs
            .bunker_url
            .split("secret=")
            .nth(1)
            .unwrap()
            .to_string();
        let r = ask(
            &s,
            &theirs.bunker_pubkey,
            &client,
            "connect",
            json!(["", secret]),
        );
        assert_eq!(
            r["error"],
            "unauthorized: Client key is linked to another account"
        );
        assert!(s
            .store
            .connection(&theirs.id)
            .unwrap()
            .unwrap()
            .client_pubkey
            .is_none());
        // a free key takes it; that records the key, so it is this account's from now on
        let free = Keys::generate();
        let r = ask(
            &s,
            &theirs.bunker_pubkey,
            &free,
            "connect",
            json!(["", secret]),
        );
        assert_eq!(r["result"], "ack");
        let r = ask(
            &s,
            &theirs.bunker_pubkey,
            &free,
            "connect",
            json!(["", secret]),
        );
        assert_eq!(r["result"], "ack", "the bound client connects again");
        let row = s.store.connection(&theirs.id).unwrap().unwrap();
        assert_eq!(row.secret_hash, "", "the secret is spent");
        s.revoke("guest-2", None, Some(&theirs.bunker_pubkey), None)
            .unwrap();
        assert!(matches!(
            s.create_connection(
                "odoo-partner-1",
                "Chat",
                "",
                &[],
                None,
                Some(&free.public_key().to_hex())
            ),
            Err(Error::ClientKeyTaken)
        ));
        // its own account's other connection: fine
        let (own, _) = s
            .create_connection("odoo-partner-1", "Notes", "", &[], None, None)
            .unwrap();
        let secret = own.bunker_url.split("secret=").nth(1).unwrap().to_string();
        let r = ask(
            &s,
            &own.bunker_pubkey,
            &client,
            "connect",
            json!(["", secret]),
        );
        assert_eq!(r["result"], "ack");
    }

    #[test]
    fn an_outside_apps_connection_signs_within_the_limits_and_an_own_one_signs_anything() {
        use crate::signer::ConnectionSpec;
        let mut s = two_accounts("limits");
        s.own = limits::Own::from_config("belfry.test", "", "", "");
        let connect = |scope: &'static str| {
            let client = Keys::generate();
            let hex = client.public_key().to_hex();
            let (c, _) = s
                .create_scoped(
                    "odoo-partner-1",
                    &ConnectionSpec {
                        app: "Notes",
                        label: "",
                        derive: &[],
                        keep: None,
                        client_pubkey: Some(&hex),
                        scope: Some(scope),
                        ..Default::default()
                    },
                )
                .unwrap();
            assert_eq!(c.scope, scope);
            (c, client)
        };
        let templates = [
            json!({"kind": 9000, "content": "", "tags": [["h", "general"]]}),
            json!({"kind": 27235, "content": "",
                   "tags": [["u", "https://belfry.belfry.test/api/v1/whois"], ["method", "POST"]]}),
            json!({"kind": 1, "content": "hello", "tags": []}),
        ];
        let sign = |c: &crate::signer::NewBunker, client: &Keys, template: &Value| {
            ask(
                &s,
                &c.bunker_pubkey,
                client,
                "sign_event",
                json!([template.to_string()]),
            )
        };
        let (outside, app) = connect("outside");
        for template in &templates[..2] {
            let r = sign(&outside, &app, template);
            assert_eq!(
                (r["result"].as_str(), r["error"].as_str()),
                (Some(""), Some("not allowed for this app")),
                "{template}"
            );
        }
        let r = sign(&outside, &app, &templates[2]);
        let ev = Event::from_json(r["result"].as_str().unwrap()).unwrap();
        ev.verify().unwrap();
        assert_eq!(ev.pubkey.to_hex(), outside.pubkey);
        // a NIP-98 for somewhere else is the app's own business; the rest is not limited
        let elsewhere =
            json!({"kind": 27235, "content": "", "tags": [["u", "https://example.org/x"]]});
        assert!(sign(&outside, &app, &elsewhere).get("error").is_none());
        let r = sign(&outside, &app, &json!("not an event"));
        assert_eq!(r["error"], "not allowed for this app");
        let peer = Keys::generate().public_key().to_hex();
        let r = ask(
            &s,
            &outside.bunker_pubkey,
            &app,
            "nip44_encrypt",
            json!([peer, "psst"]),
        );
        assert!(r.get("error").is_none(), "{r}");
        assert_eq!(
            ask(
                &s,
                &outside.bunker_pubkey,
                &app,
                "get_public_key",
                json!([])
            )["result"],
            outside.pubkey
        );

        let (own, chat) = connect("own");
        for template in &templates {
            let r = sign(&own, &chat, template);
            assert!(r.get("error").is_none(), "{template}: {r}");
            Event::from_json(r["result"].as_str().unwrap())
                .unwrap()
                .verify()
                .unwrap();
        }
        // and a connection made without a scope is an own one
        let (plain, _) = s
            .create_connection("odoo-partner-1", "Chat", "", &[], None, None)
            .unwrap();
        assert_eq!(s.store.connection(&plain.id).unwrap().unwrap().scope, "own");
        assert!(matches!(
            s.create_scoped(
                "odoo-partner-1",
                &ConnectionSpec {
                    app: "Notes",
                    label: "",
                    derive: &[],
                    keep: None,
                    client_pubkey: None,
                    scope: Some("everything"),
                    ..Default::default()
                },
            ),
            Err(Error::Invalid(_))
        ));
    }

    #[test]
    fn an_unclaimed_connection_is_gone_after_its_deadline() {
        let s = two_accounts("deadline");
        let (c, _) = s
            .create_connection("guest-2", "Notes", "", &[], None, None)
            .unwrap();
        let bunker_pk = PublicKey::from_hex(&c.bunker_pubkey).unwrap();
        let secret = c.bunker_url.split("secret=").nth(1).unwrap().to_string();
        s.store.set_expires_at(&c.id, store::now() - 1);
        let client = Keys::generate();
        let body = json!({"id": "1", "method": "connect", "params": ["", secret]});
        assert!(respond(&s, &request(&bunker_pk, &client, body)).is_none());
    }

    #[test]
    fn requests_for_unknown_bunkers_and_forged_ones_get_no_answer() {
        let s = two_accounts("unknown");
        let client = Keys::generate();
        let (c, _) = s
            .create_connection(
                "odoo-partner-1",
                "Chat",
                "",
                &[],
                None,
                Some(&client.public_key().to_hex()),
            )
            .unwrap();
        let bunker_pk = PublicKey::from_hex(&c.bunker_pubkey).unwrap();
        let body = json!({"id": "1", "method": "ping", "params": []});
        // a bunker key the signer does not have: dropped on the lookup, whatever the event
        let nobody = Keys::generate().public_key();
        assert!(respond(&s, &request(&nobody, &client, body.clone())).is_none());
        // a known bunker key, but the event is not signed by who it says
        let good = request(&bunker_pk, &client, body.clone());
        let other = request(&bunker_pk, &client, json!({"id": "2", "method": "ping"}));
        let mut forged: Value = serde_json::from_str(&good.as_json()).unwrap();
        forged["sig"] = json!(other.sig.to_string());
        let forged = Event::from_json(forged.to_string()).unwrap();
        assert!(respond(&s, &forged).is_none());
        assert!(respond(&s, &good).is_some());
        // not a kind 24133
        let note = EventBuilder::new(Kind::TextNote, "hi")
            .tag(Tag::public_key(bunker_pk))
            .finalize(&client)
            .unwrap();
        assert!(respond(&s, &note).is_none());
    }

    #[test]
    fn a_connection_answers_within_its_rate_limit() {
        let mut s = two_accounts("rate");
        s.limiter.client = (3.0, 0.0);
        s.limiter.others = (1.0, 0.0);
        let client = Keys::generate();
        let (c, _) = s
            .create_connection(
                "odoo-partner-1",
                "Chat",
                "",
                &[],
                None,
                Some(&client.public_key().to_hex()),
            )
            .unwrap();
        let bunker_pk = PublicKey::from_hex(&c.bunker_pubkey).unwrap();
        let ping = |who: &Keys, n: u32| {
            respond(
                &s,
                &request(
                    &bunker_pk,
                    who,
                    json!({"id": n.to_string(), "method": "ping"}),
                ),
            )
        };
        // a stranger's flood uses up the strangers' share, not the client's
        let stranger = Keys::generate();
        assert!(ping(&stranger, 0).is_some());
        assert!(ping(&stranger, 1).is_none());
        assert!(ping(&client, 2).is_some() && ping(&client, 3).is_some());
        assert!(ping(&client, 4).is_some());
        assert!(ping(&client, 5).is_none());
    }

    #[test]
    fn an_event_is_taken_in_once() {
        let seen = Seen::new(2, 100);
        let id = |n: u8| EventId::from_slice(&[n; 32]).unwrap();
        assert!(seen.first(id(1), 0));
        assert!(!seen.first(id(1), 0));
        assert!(seen.first(id(2), 0) && seen.first(id(3), 0));
        assert!(!seen.first(id(3), 0) && !seen.first(id(2), 0));
        assert!(seen.first(id(1), 0), "only the last ones are remembered");
        // and only for as long as a request could come again
        let seen = Seen::new(10, 100);
        assert!(seen.first(id(1), 1000) && seen.first(id(2), 1050));
        assert!(!seen.first(id(1), 1099));
        assert!(seen.first(id(1), 1100), "forgotten after its time");
        assert!(!seen.first(id(2), 1149));
        assert!(seen.first(id(2), 1150));
    }

    /// A request as `client` would send it, stamped `created_at`.
    fn request_at(bunker_pk: &PublicKey, client: &Keys, body: Value, created_at: u64) -> Event {
        let content = nip44::encrypt(
            client.secret_key(),
            bunker_pk,
            body.to_string(),
            nip44::Version::V2,
        )
        .unwrap();
        EventBuilder::new(Kind::NostrConnect, content)
            .tag(Tag::public_key(*bunker_pk))
            .custom_created_at(Timestamp::from_secs(created_at))
            .finalize(client)
            .unwrap()
    }

    #[test]
    fn a_request_is_answered_once_and_only_when_it_is_from_now() {
        let s = two_accounts("fresh");
        let client = Keys::generate();
        let (c, _) = s
            .create_connection(
                "odoo-partner-1",
                "Chat",
                "",
                &[],
                None,
                Some(&client.public_key().to_hex()),
            )
            .unwrap();
        let bunker_pk = PublicKey::from_hex(&c.bunker_pubkey).unwrap();
        let ping = json!({"id": "1", "method": "ping", "params": []});
        // the same event again (a relay that sends it twice, or somebody who recorded it)
        let once = request(&bunker_pk, &client, ping.clone());
        assert!(respond(&s, &once).is_some());
        assert!(
            respond(&s, &once).is_none(),
            "a repeat gets no second answer"
        );
        let before = s.store.connection(&c.id).unwrap().unwrap().uses;
        assert!(respond(&s, &once).is_none());
        assert_eq!(s.store.connection(&c.id).unwrap().unwrap().uses, before);
        // stamped too long ago, or too far ahead: no answer, and nothing remembered or spent
        let now = store::now() as u64;
        for at in [now - FRESH as u64 - 5, now + FRESH as u64 + 5, 0] {
            let stale = request_at(&bunker_pk, &client, ping.clone(), at);
            stale.verify().unwrap();
            assert!(respond(&s, &stale).is_none(), "{at}");
        }
        // within the window, either way, it is answered: once
        for at in [now - FRESH as u64 + 30, now + FRESH as u64 - 30] {
            let near = request_at(&bunker_pk, &client, ping.clone(), at);
            assert!(respond(&s, &near).is_some(), "{at}");
            assert!(respond(&s, &near).is_none(), "{at}");
        }
        // what the look-back of a renewed subscription brings is always within the window
        const { assert!((LOOK_BACK as i64) < FRESH && SEEN_FOR > 2 * FRESH) };
    }

    #[test]
    fn a_forged_request_spends_nothing_of_the_clients_allowance() {
        let mut s = two_accounts("forged-rate");
        s.limiter.client = (2.0, 0.0);
        s.limiter.others = (1.0, 0.0);
        let client = Keys::generate();
        let (c, _) = s
            .create_connection(
                "odoo-partner-1",
                "Chat",
                "",
                &[],
                None,
                Some(&client.public_key().to_hex()),
            )
            .unwrap();
        let bunker_pk = PublicKey::from_hex(&c.bunker_pubkey).unwrap();
        // events that name the client as their author, with a signature that is not theirs
        let borrowed = request(&bunker_pk, &client, json!({"id": "s", "method": "ping"}));
        for n in 0..10 {
            let good = request(
                &bunker_pk,
                &client,
                json!({"id": n.to_string(), "method": "ping"}),
            );
            let mut forged: Value = serde_json::from_str(&good.as_json()).unwrap();
            forged["sig"] = json!(borrowed.sig.to_string());
            let forged = Event::from_json(forged.to_string()).unwrap();
            assert!(respond(&s, &forged).is_none());
            // not remembered either: the real request with that id is still answered
            if n == 0 {
                assert!(respond(&s, &good).is_some());
            }
        }
        // the client still has the rest of its allowance (2, one used above)
        let ping = |n: u32| {
            respond(
                &s,
                &request(
                    &bunker_pk,
                    &client,
                    json!({"id": format!("c{n}"), "method": "ping"}),
                ),
            )
        };
        assert!(ping(1).is_some(), "ten forgeries spent nothing");
        assert!(ping(2).is_none(), "its own requests did");
    }

    #[test]
    fn an_outside_app_decrypts_only_with_the_members_consent() {
        use crate::signer::ConnectionSpec;
        let s = two_accounts("consent");
        let account = s.keys("odoo-partner-1").unwrap();
        let peer = Keys::generate();
        let nip44_text = nip44::encrypt(
            peer.secret_key(),
            &account.public_key(),
            "for the member",
            nip44::Version::V2,
        )
        .unwrap();
        let nip04_text =
            nip04::encrypt(peer.secret_key(), &account.public_key(), "for the member").unwrap();
        let connect = |scope: &'static str, decrypt: bool| {
            let client = Keys::generate();
            let hex = client.public_key().to_hex();
            let (c, _) = s
                .create_scoped(
                    "odoo-partner-1",
                    &ConnectionSpec {
                        app: "Notes",
                        client_pubkey: Some(&hex),
                        scope: Some(scope),
                        decrypt,
                        ..Default::default()
                    },
                )
                .unwrap();
            assert_eq!(c.decrypt, decrypt);
            (c, client)
        };
        let peer_hex = peer.public_key().to_hex();
        let read = |c: &crate::signer::NewBunker, client: &Keys| {
            [
                ("nip44_decrypt", &nip44_text),
                ("nip04_decrypt", &nip04_text),
            ]
            .map(|(m, text)| ask(&s, &c.bunker_pubkey, client, m, json!([peer_hex, text])))
        };
        // an outside app without the member's word: it writes, it does not read
        let (outside, app) = connect("outside", false);
        for r in read(&outside, &app) {
            assert_eq!(
                (r["result"].as_str(), r["error"].as_str()),
                (Some(""), Some("not allowed for this app"))
            );
        }
        for method in ["nip44_encrypt", "nip04_encrypt"] {
            let r = ask(
                &s,
                &outside.bunker_pubkey,
                &app,
                method,
                json!([peer_hex, "hi"]),
            );
            assert!(r.get("error").is_none(), "{method}: {r}");
        }
        // the member says yes later (the API's consent call), and no again
        s.set_decrypt("odoo-partner-1", Some(&outside.id), None, true)
            .unwrap();
        for r in read(&outside, &app) {
            assert_eq!(r["result"], "for the member", "{r}");
        }
        s.set_decrypt("odoo-partner-1", None, Some(&outside.bunker_pubkey), false)
            .unwrap();
        assert_eq!(read(&outside, &app)[0]["error"], "not allowed for this app");
        assert!(matches!(
            s.set_decrypt("guest-2", Some(&outside.id), None, true),
            Err(Error::NotFound)
        ));
        assert!(matches!(
            s.set_decrypt("odoo-partner-1", None, None, true),
            Err(Error::Invalid(_))
        ));
        // consent given when the connection is made; an own app needs none
        let (allowed, app2) = connect("outside", true);
        let (own, chat) = connect("own", false);
        for (c, client) in [(&allowed, &app2), (&own, &chat)] {
            for r in read(c, client) {
                assert_eq!(r["result"], "for the member", "{r}");
            }
        }
        // and the belfry's own names stay out of an outside app's reach, consent or not
        for template in [
            json!({"kind": 30078, "content": "", "tags": [["d", "belfry:sealed"]]}),
            json!({"kind": 1985, "content": "", "tags": [["L", "belfry/pin"], ["l", "pinned", "belfry/pin"]]}),
        ] {
            let r = ask(
                &s,
                &allowed.bunker_pubkey,
                &app2,
                "sign_event",
                json!([template.to_string()]),
            );
            assert_eq!(r["error"], "not allowed for this app", "{template}");
            let r = ask(
                &s,
                &own.bunker_pubkey,
                &chat,
                "sign_event",
                json!([template.to_string()]),
            );
            assert!(r.get("error").is_none(), "{template}: {r}");
        }
        for template in [
            json!({"kind": 30078, "content": "", "tags": [["d", "notes:settings"]]}),
            json!({"kind": 5, "content": "", "tags": [["e", "00".repeat(32)]]}),
        ] {
            let r = ask(
                &s,
                &allowed.bunker_pubkey,
                &app2,
                "sign_event",
                json!([template.to_string()]),
            );
            assert!(r.get("error").is_none(), "{template}: {r}");
        }
    }

    #[test]
    fn a_sign_in_over_http_takes_a_client_in_time_and_none_after() {
        use crate::signer::ConnectionSpec;
        let s = two_accounts("http");
        let make = || {
            s.create_scoped(
                "odoo-partner-1",
                &ConnectionSpec {
                    app: "Wallet",
                    kind: Some("http"),
                    origin: "https://wallet.belfry.test",
                    ..Default::default()
                },
            )
            .unwrap()
            .0
        };
        let c = make();
        assert_eq!(
            (c.kind.as_str(), c.origin.as_str()),
            ("http", "https://wallet.belfry.test")
        );
        assert!(c.expires_at.is_some(), "its secret has a deadline");
        let secret = |c: &crate::signer::NewBunker| {
            c.bunker_url.split("secret=").nth(1).unwrap().to_string()
        };
        // in time: an app that does talk NIP-46 connects as on any connection
        let app = Keys::generate();
        let r = ask(
            &s,
            &c.bunker_pubkey,
            &app,
            "connect",
            json!(["", secret(&c)]),
        );
        assert_eq!(r["result"], "ack");
        assert_eq!(
            ask(&s, &c.bunker_pubkey, &app, "ping", json!([]))["result"],
            "pong"
        );
        // nobody connected in time: the sign-in stays (the API signs through it), the bunker
        // is silent on it and its secret opens nothing
        let c = make();
        s.store.set_expires_at(&c.id, store::now() - 1);
        let bunker_pk = PublicKey::from_hex(&c.bunker_pubkey).unwrap();
        let body = json!({"id": "1", "method": "connect", "params": ["", secret(&c)]});
        assert!(respond(&s, &request(&bunker_pk, &app, body)).is_none());
        let row = s.store.connection(&c.id).unwrap().expect("still there");
        assert!(row.client_pubkey.is_none());
        assert_eq!(
            handle(
                &s,
                &row,
                &app.public_key(),
                "connect",
                &[json!(""), json!(secret(&c))]
            ),
            Err("unauthorized: this connection has ended".to_string())
        );
        assert!(!s
            .store
            .live_bunker_pubkeys()
            .unwrap()
            .contains(&c.bunker_pubkey));
        assert_eq!(s.expire_unclaimed(), 1);
        assert_eq!(s.connections("odoo-partner-1").unwrap().len(), 2);
        let (keys, via) = s.keys_via("odoo-partner-1", Some(&c.id)).unwrap();
        assert_eq!(via.unwrap().kind, "http");
        s.sign(&keys, &json!({"kind": 1, "content": "over http"}))
            .unwrap();
        // an unknown kind is refused
        assert!(matches!(
            s.create_scoped(
                "odoo-partner-1",
                &ConnectionSpec {
                    app: "Wallet",
                    kind: Some("carrier-pigeon"),
                    ..Default::default()
                },
            ),
            Err(Error::Invalid(_))
        ));
    }

    #[test]
    fn a_failed_subscription_is_tried_again_soon_and_then_with_a_growing_pause() {
        let t0 = Instant::now();
        let secs = Duration::from_secs;
        let mut r = Renewal::new(t0);
        assert!(!r.subscribed && r.due(t0, true), "the first round is due");
        assert!(!r.due(t0, false), "not without a relay");
        // it took: next in five minutes
        r.record(true, t0);
        assert!(r.subscribed);
        assert!(!r.due(t0 + secs(299), true) && r.due(t0 + RENEW, true));
        // a round fails: not subscribed from that moment, and the next tick tries again
        r.record(false, t0);
        assert!(!r.subscribed, "a failed round is not a subscription");
        assert!(r.due(t0, true));
        // then 5, 10, 20, 40, 60, 60 seconds
        let mut at = t0;
        for pause in [5, 10, 20, 40, 60, 60] {
            r.record(false, at);
            assert!(!r.subscribed);
            assert!(!r.due(at + secs(pause - 1), true), "{pause}");
            assert!(r.due(at + secs(pause), true), "{pause}");
            at += secs(pause);
        }
        // one that takes starts over
        r.record(true, at);
        assert!(r.subscribed && !r.due(at + secs(60), true));
        r.record(false, at);
        assert!(
            r.due(at, true),
            "the first failure after a success: the next tick"
        );
    }

    #[test]
    fn the_relay_is_asked_for_the_live_bunker_keys_only() {
        let nobody = Keys::generate().public_key();
        let since = Timestamp::from_secs(1_700_000_000);
        let (filters, filtered) = filters_for(&[], &nobody, since);
        assert!(filtered);
        let f: Value = serde_json::from_str(&filters[0].as_json()).unwrap();
        assert_eq!(
            f["#p"],
            json!([nobody.to_hex()]),
            "no connection: nobody's requests"
        );
        assert_eq!(
            (f["kinds"].clone(), f["since"].clone()),
            (json!([24133]), json!(1_700_000_000))
        );

        let keys: Vec<String> = (0..KEYS_PER_FILTER + 1)
            .map(|_| Keys::generate().public_key().to_hex())
            .collect();
        let (filters, filtered) = filters_for(&keys, &nobody, since);
        assert!(filtered);
        assert_eq!(filters.len(), 2);
        let all: usize = filters
            .iter()
            .map(|f| {
                let f: Value = serde_json::from_str(&f.as_json()).unwrap();
                f["#p"].as_array().unwrap().len()
            })
            .sum();
        assert_eq!(all, keys.len());

        let many = vec![keys[0].clone(); MAX_KEYS + 1];
        let (filters, filtered) = filters_for(&many, &nobody, since);
        assert!(!filtered);
        let f: Value = serde_json::from_str(&filters[0].as_json()).unwrap();
        assert!(f.get("#p").is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn the_loop_starts_again_when_it_ends_or_panics() {
        use std::sync::atomic::AtomicUsize;
        let s = Arc::new(two_accounts("supervise"));
        s.bunker.connected.store(true, Ordering::SeqCst);
        let runs = Arc::new(AtomicUsize::new(0));
        let counter = runs.clone();
        let supervisor = tokio::spawn(supervise(s.clone(), move || {
            let n = counter.fetch_add(1, Ordering::SeqCst);
            async move {
                match n {
                    0 => panic!("a bug in the loop"),
                    1 => "the stream ended",
                    _ => std::future::pending().await,
                }
            }
        }));
        // 1 s after the panic, 2 s after the stream ended: the third run stays
        tokio::time::sleep(Duration::from_secs(10)).await;
        assert_eq!(runs.load(Ordering::SeqCst), 3);
        assert_eq!(s.bunker.restarts.load(Ordering::SeqCst), 2);
        assert!(!s.bunker.connected.load(Ordering::SeqCst));
        supervisor.abort();
    }

    #[tokio::test]
    async fn a_new_connection_waits_for_the_subscription_that_covers_it() {
        let s = Arc::new(two_accounts("wait"));
        // bunker off: no waiting, and nobody was promised a listener
        let (_, n) = s
            .create_connection("guest-2", "Notes", "", &[], None, None)
            .unwrap();
        tokio::time::timeout(
            Duration::from_millis(200),
            s.wait_subscribed(n, Duration::from_secs(30)),
        )
        .await
        .expect("returns at once without a bunker")
        .expect("and that is fine");
        s.bunker.enabled.store(true, Ordering::SeqCst);
        // a bunker that is not connected: the wait is bounded and ends in an error, never in
        // "made" for a connection nobody listens on
        let down = tokio::time::timeout(
            Duration::from_secs(2),
            s.wait_subscribed(n, Duration::from_millis(50)),
        )
        .await
        .expect("bounded");
        assert!(matches!(down, Err(Error::BunkerUnavailable)));
        s.bunker.connected.store(true, Ordering::SeqCst);
        let waiter = {
            let s = s.clone();
            tokio::spawn(async move { s.wait_subscribed(n, Duration::from_secs(30)).await })
        };
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(!waiter.is_finished());
        s.bunker.subscribed.send_replace(n);
        tokio::time::timeout(Duration::from_secs(2), waiter)
            .await
            .expect("woken by the subscription")
            .unwrap()
            .expect("covered");
        // it comes back while the wait runs (the relay reconnects): heard, not an error
        let late = {
            let s = s.clone();
            tokio::spawn(async move { s.wait_subscribed(n + 1, Duration::from_secs(30)).await })
        };
        tokio::time::sleep(Duration::from_millis(20)).await;
        s.bunker.subscribed.send_replace(n + 1);
        assert!(late.await.unwrap().is_ok());
        // and a subscription that never covers it: an error after the timeout
        let never = tokio::time::timeout(
            Duration::from_secs(2),
            s.wait_subscribed(n + 2, Duration::from_millis(50)),
        )
        .await
        .expect("gives up");
        assert!(matches!(never, Err(Error::BunkerUnavailable)));
    }

    /// The whole loop against a real relay. Not part of `cargo test`: run it with a relay
    /// that passes kind 24133 on, `BUNKER_TEST_RELAY=ws://127.0.0.1:7778 cargo test -- --ignored`.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore]
    async fn over_a_relay_a_client_is_heard_the_moment_its_connection_exists() {
        let relay = std::env::var("BUNKER_TEST_RELAY").expect("BUNKER_TEST_RELAY");
        let _ = tracing_subscriber::fmt().with_test_writer().try_init();
        let s = Arc::new(two_accounts("relay"));
        tokio::spawn(run(s.clone(), vec![relay.clone()]));
        for _ in 0..100 {
            if s.bunker.connected.load(Ordering::SeqCst) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        assert!(
            s.bunker.connected.load(Ordering::SeqCst),
            "the bunker reached the relay"
        );
        assert!(s.bunker.filtered.load(Ordering::SeqCst));

        let app_keys = Keys::generate();
        let app = Client::default();
        app.add_relay(relay.as_str()).await.unwrap();
        app.connect().and_wait(Duration::from_secs(5)).await;
        let (tx, mut answers) = tokio::sync::mpsc::unbounded_channel::<Event>();
        let mut notifications = app.notifications();
        tokio::spawn(async move {
            while let Some(n) = notifications.next().await {
                if let ClientNotification::Event { event, .. } = n {
                    let _ = tx.send(*event);
                }
            }
        });
        app.subscribe(
            Filter::new()
                .kind(Kind::NostrConnect)
                .pubkey(app_keys.public_key())
                .since(Timestamp::now()),
        )
        .await
        .unwrap();

        async fn call(
            app: &Client,
            answers: &mut tokio::sync::mpsc::UnboundedReceiver<Event>,
            keys: &Keys,
            bunker: &str,
            id: &str,
            method: &str,
            params: Value,
        ) -> Option<Value> {
            let bunker_pk = PublicKey::from_hex(bunker).unwrap();
            let body = json!({"id": id, "method": method, "params": params});
            app.send_event(&request(&bunker_pk, keys, body))
                .await
                .unwrap();
            let wait = async {
                while let Some(event) = answers.recv().await {
                    if event.pubkey != bunker_pk {
                        continue;
                    }
                    let plain =
                        nip44::decrypt(keys.secret_key(), &bunker_pk, &event.content).ok()?;
                    let v: Value = serde_json::from_str(&plain).ok()?;
                    if v["id"] == id {
                        return Some(v);
                    }
                }
                None
            };
            tokio::time::timeout(Duration::from_secs(3), wait)
                .await
                .ok()
                .flatten()
        }

        // a connection made now is answered at once: the API waits for the subscription
        let hex = app_keys.public_key().to_hex();
        let (c1, n) = s
            .create_connection("odoo-partner-1", "Chat", "", &[], None, Some(&hex))
            .unwrap();
        s.wait_subscribed(n, Duration::from_millis(1500))
            .await
            .expect("the subscription covers it");
        assert!(
            *s.bunker.subscribed.borrow() >= n,
            "the subscription covers it"
        );
        let r = call(
            &app,
            &mut answers,
            &app_keys,
            &c1.bunker_pubkey,
            "a",
            "connect",
            json!([]),
        )
        .await
        .expect("an answer to connect");
        assert_eq!(r["result"], "ack");
        let r = call(
            &app,
            &mut answers,
            &app_keys,
            &c1.bunker_pubkey,
            "b",
            "get_public_key",
            json!([]),
        )
        .await
        .expect("an answer");
        assert_eq!(r["result"], c1.pubkey);
        assert!(s.bunker.last_event_at.load(Ordering::SeqCst) > 0);

        // a second one, by its secret, while the first keeps working
        let (c2, n) = s
            .create_connection("odoo-partner-1", "Notes", "", &[], None, None)
            .unwrap();
        s.wait_subscribed(n, Duration::from_millis(1500))
            .await
            .expect("the subscription covers it");
        let secret = c2.bunker_url.split("secret=").nth(1).unwrap().to_string();
        let r = call(
            &app,
            &mut answers,
            &app_keys,
            &c2.bunker_pubkey,
            "c",
            "connect",
            json!(["", secret]),
        )
        .await
        .expect("an answer on the new connection");
        assert_eq!(r["result"], "ack");
        let r = call(
            &app,
            &mut answers,
            &app_keys,
            &c1.bunker_pubkey,
            "d",
            "ping",
            json!([]),
        )
        .await
        .expect("the first still answers");
        assert_eq!(r["result"], "pong");

        // revoked: silence
        s.revoke("odoo-partner-1", Some(&c1.id), None, None)
            .unwrap();
        assert!(call(
            &app,
            &mut answers,
            &app_keys,
            &c1.bunker_pubkey,
            "e",
            "ping",
            json!([])
        )
        .await
        .is_none());
        let r = call(
            &app,
            &mut answers,
            &app_keys,
            &c2.bunker_pubkey,
            "f",
            "ping",
            json!([]),
        )
        .await
        .expect("the other one goes on");
        assert_eq!(r["result"], "pong");
        assert_eq!(s.bunker.restarts.load(Ordering::SeqCst), 0);
    }
}
