//! The NIP-46 bunker: answers remote-signing requests on the bunker relay.
//!
//! Every connection has a key of its own (the `bunker://<key>` in its URL), so one connection
//! can be revoked without touching the others, and its secret binds it to the first client key
//! that presents it in `connect`. Requests are NIP-44 (or NIP-04, for older clients)
//! encrypted kind-24133 events p-tagged to the connection's key; the answer goes back the same
//! way. Methods: connect, get_public_key, sign_event, nip04/nip44 encrypt and decrypt, ping,
//! get_relays, and Belfry's derive_secret for the purposes the connection was given.

use std::sync::Arc;
use std::time::Duration;

use nostr::nips::{nip04, nip44};
use nostr::prelude::{Event, EventBuilder, Filter, FinalizeEvent, Keys, Kind, PublicKey, Tag};
use nostr_sdk::prelude::{Client, ClientNotification, StreamExt};
use serde_json::{json, Value};
use subtle::ConstantTimeEq;

use crate::signer::{Error, Scheme, Signer};
use crate::store::ConnectionRow;

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
        return match &conn.client_pubkey {
            Some(bound) if *bound == sender_hex => Ok("ack".into()),
            Some(_) => Err("unauthorized: this connection belongs to another client".into()),
            None => {
                let secret = params.get(1).and_then(Value::as_str).unwrap_or("");
                if !secret.is_empty() && bool::from(secret.as_bytes().ct_eq(conn.secret.as_bytes()))
                {
                    signer
                        .store
                        .bind_client(&conn.id, &sender_hex)
                        .map_err(|e| e.to_string())?;
                    tracing::info!(connection = %conn.id, app = %conn.app, "bunker client connected");
                    Ok("ack".into())
                } else {
                    Err("unauthorized: wrong or missing secret".into())
                }
            }
        };
    }
    if conn.client_pubkey.as_deref() != Some(sender_hex.as_str()) {
        return Err("unauthorized: connect first".into());
    }
    if method == "ping" {
        return Ok("pong".into());
    }
    let account = signer.account(&conn.vine_id).map_err(err_text)?;
    let keys = signer.keys_of(&account).map_err(err_text)?;
    let _ = signer.store.touch_connection(&conn.id);
    match method {
        "get_public_key" => Ok(keys.public_key().to_hex()),
        "get_relays" => Ok("{}".into()),
        "sign_event" => {
            let template: Value = serde_json::from_str(&param(params, 0)?)
                .map_err(|_| "sign_event: the event is not JSON".to_string())?;
            let event = signer.sign(&keys, &template).map_err(err_text)?;
            Ok(event.as_json())
        }
        "nip44_encrypt" | "nip04_encrypt" | "nip44_decrypt" | "nip04_decrypt" => {
            let scheme = if method.starts_with("nip44") {
                Scheme::Nip44
            } else {
                Scheme::Nip04
            };
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

/// Turns one kind-24133 event into the response event, or None when it is not for a live
/// connection (or cannot be read).
pub fn respond(signer: &Signer, event: &Event) -> Option<Event> {
    let bunker_hex = event.tags.public_keys().next()?.to_hex();
    let conn = signer.store.connection_by_bunker(&bunker_hex).ok()??;
    let bunker = signer.bunker_keys(&conn).ok()?;
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

/// Runs the bunker until the process ends: subscribes to new kind-24133 events on the bunker
/// relays and answers those for live connections.
pub async fn run(signer: Arc<Signer>, relays: Vec<String>) {
    if relays.is_empty() {
        tracing::warn!("no BUNKER_RELAYS: the NIP-46 bunker is off");
        return;
    }
    let client = Client::default();
    for relay in &relays {
        if let Err(e) = client.add_relay(relay.as_str()).await {
            tracing::error!(relay, "bunker relay: {e}");
        }
    }
    client.connect().await;
    // limit 0: only requests from now on; a request is never answered twice after a reconnect
    let filter = Filter::new().kind(Kind::NostrConnect).limit(0);
    loop {
        match client.subscribe(filter.clone()).await {
            Ok(_) => break,
            Err(e) => {
                tracing::warn!("bunker subscription failed ({e}); retrying");
                tokio::time::sleep(Duration::from_secs(3)).await;
            }
        }
    }
    tracing::info!(?relays, "bunker listening");
    let mut notifications = client.notifications();
    while let Some(notification) = notifications.next().await {
        let ClientNotification::Event { event, .. } = notification else {
            continue;
        };
        if event.kind != Kind::NostrConnect || event.verify().is_err() {
            continue;
        }
        let signer = signer.clone();
        let answer = tokio::task::spawn_blocking(move || respond(&signer, &event)).await;
        if let Ok(Some(answer)) = answer {
            if let Err(e) = client.send_event(&answer).await {
                tracing::warn!("bunker answer not sent: {e}");
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
        let account = s.ensure_account("odoo-partner-42", 0, None, None).unwrap();
        let c = s
            .create_connection(
                "odoo-partner-42",
                "belfry-wallet",
                "Wallet",
                &["cashu-wallet-v1".into()],
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

        s.revoke("odoo-partner-42", Some(&c.id), None).unwrap();
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
        s.ensure_account("odoo-partner-1", 0, None, None).unwrap();
        let c = s
            .create_connection("odoo-partner-1", "outside", "Amethyst", &[], None)
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
}
