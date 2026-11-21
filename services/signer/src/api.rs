//! The internal HTTP API. Only the onboarding service calls it (Compose network `signer`, no
//! published port), with the bearer token from `/run/secrets/signer_token`.
//!
//! ```text
//! GET  /health                                  {status, seed, bunker}     (no token)
//! PUT  /accounts/{vine_id}                      {key_version?, own_pubkey?, expect_pubkey?, username?}
//! GET  /accounts/{vine_id}
//! POST /accounts/{vine_id}/status               {status: active|suspended}
//! POST /accounts/{vine_id}/own-key              {pubkey}        (the move to the member's key)
//! PUT  /accounts/{vine_id}/username             {username}
//! POST /accounts/{vine_id}/sign                 {event}                    -> {event}
//! POST /accounts/{vine_id}/encrypt              {scheme: nip44|nip04, peer, text} -> {result}
//! POST /accounts/{vine_id}/decrypt              {scheme, peer, text}       -> {result}
//! POST /accounts/{vine_id}/derive               {purpose}                  -> {secret}
//! POST /accounts/{vine_id}/export                                          -> {nsec}
//! POST /accounts/{vine_id}/connections          {app, label?, client_pubkey?, derive?, keep?}
//! GET  /accounts/{vine_id}/connections                                     -> [{id, app, ...}]
//! POST /accounts/{vine_id}/connections/revoke   {id? | bunker_pubkey? | app?} -> {revoked}
//! GET  /names/{username}                                                   -> {name, pubkey, vine_id}
//! POST /pubkeys/{pubkey}/status                 {status}                   -> {updated}
//! POST /import                                  {accounts: [{vine_id, pubkey, sealed}]}
//! ```
//!
//! sign, encrypt, decrypt and derive take an optional `connection` (the id behind an app's
//! access token): it must be live, and derive allows only that connection's purposes.
//!
//! Errors are `{"error": <code>, "message"}` with 404 not_found, 503 seed_locked,
//! 409 own_key / mismatch / client_key_taken / username_taken, 403 suspended / forbidden,
//! 422 invalid, 401 unauthorized.

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use subtle::ConstantTimeEq;

use crate::signer::{Error, Scheme, Signer};

#[derive(Clone)]
pub struct AppState {
    pub signer: Arc<Signer>,
    pub token: Arc<String>,
}

pub struct ApiError(Error);

impl From<Error> for ApiError {
    fn from(e: Error) -> Self {
        ApiError(e)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = match &self.0 {
            Error::NotFound => StatusCode::NOT_FOUND,
            Error::SeedLocked => StatusCode::SERVICE_UNAVAILABLE,
            Error::OwnKey | Error::Mismatch(_) | Error::ClientKeyTaken | Error::UsernameTaken => {
                StatusCode::CONFLICT
            }
            Error::Suspended | Error::Forbidden(_) => StatusCode::FORBIDDEN,
            Error::Invalid(_) => StatusCode::UNPROCESSABLE_ENTITY,
            Error::SeedMalformed | Error::Internal(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };
        if status.is_server_error() {
            tracing::error!(code = self.0.code(), "{}", self.0.message());
        }
        (
            status,
            Json(json!({"error": self.0.code(), "message": self.0.message()})),
        )
            .into_response()
    }
}

#[allow(clippy::result_large_err)]
fn authorized(state: &AppState, headers: &HeaderMap) -> Result<(), Response> {
    let given = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("");
    if !state.token.is_empty() && bool::from(given.as_bytes().ct_eq(state.token.as_bytes())) {
        Ok(())
    } else {
        Err((
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthorized", "message": "Bearer token required."})),
        )
            .into_response())
    }
}

macro_rules! guard {
    ($state:expr, $headers:expr) => {
        if let Err(resp) = authorized(&$state, &$headers) {
            return Ok(resp);
        }
    };
}

fn text(body: &Value, key: &str) -> Result<String, ApiError> {
    body.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| ApiError(Error::Invalid(format!("{key} must be text"))))
}

fn opt_text(body: &Value, key: &str) -> Option<String> {
    body.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

fn account_json(a: &crate::store::Account) -> Value {
    json!({
        "vine_id": a.vine_id,
        "pubkey": a.pubkey,
        "custody": a.custody,
        "key_version": a.key_version,
        "status": a.status,
        "username": a.username,
    })
}

/// No token, and nothing about who is here: whether the signer can make accounts (the seed)
/// and whether its bunker hears the relay.
async fn health(State(state): State<AppState>) -> Json<Value> {
    let b = &state.signer.bunker;
    let enabled = b.enabled.load(Ordering::SeqCst);
    let connected = b.connected.load(Ordering::SeqCst);
    let last = b.last_event_at.load(Ordering::SeqCst);
    let bunker = json!({
        "state": if !enabled { "off" } else if connected { "connected" } else { "disconnected" },
        "last_event_at": if last > 0 { json!(last) } else { Value::Null },
        "restarts": b.restarts.load(Ordering::SeqCst),
        "filtered": b.filtered.load(Ordering::SeqCst),
    });
    let ok = !enabled || connected;
    Json(json!({
        "status": if ok { "ok" } else { "degraded" },
        "seed": state.signer.seed_state(),
        "bunker": bunker,
    }))
}

async fn put_account(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> Result<Response, ApiError> {
    guard!(state, headers);
    let version = body.get("key_version").and_then(Value::as_u64).unwrap_or(0);
    let version =
        u32::try_from(version).map_err(|_| Error::Invalid("key_version too big".into()))?;
    let a = state.signer.ensure_account(
        &vine_id,
        version,
        opt_text(&body, "own_pubkey").as_deref(),
        opt_text(&body, "expect_pubkey").as_deref(),
        opt_text(&body, "username").as_deref(),
    )?;
    Ok(Json(account_json(&a)).into_response())
}

async fn get_account(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
) -> Result<Response, ApiError> {
    guard!(state, headers);
    Ok(Json(account_json(&state.signer.account(&vine_id)?)).into_response())
}

async fn set_status(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> Result<Response, ApiError> {
    guard!(state, headers);
    state.signer.set_status(&vine_id, &text(&body, "status")?)?;
    Ok(Json(json!({"ok": true})).into_response())
}

async fn own_key(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> Result<Response, ApiError> {
    guard!(state, headers);
    let a = state
        .signer
        .move_to_own_key(&vine_id, &text(&body, "pubkey")?)?;
    Ok(Json(account_json(&a)).into_response())
}

async fn set_username(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> Result<Response, ApiError> {
    guard!(state, headers);
    let a = state
        .signer
        .set_username(&vine_id, &text(&body, "username")?)?;
    Ok(Json(account_json(&a)).into_response())
}

async fn name(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(username): Path<String>,
) -> Result<Response, ApiError> {
    guard!(state, headers);
    let a = state.signer.by_username(&username)?;
    Ok(Json(json!({"name": a.username, "pubkey": a.pubkey, "vine_id": a.vine_id})).into_response())
}

async fn status_by_pubkey(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(pubkey): Path<String>,
    Json(body): Json<Value>,
) -> Result<Response, ApiError> {
    guard!(state, headers);
    let n = state
        .signer
        .set_status_by_pubkey(&pubkey, &text(&body, "status")?)?;
    Ok(Json(json!({"updated": n})).into_response())
}

async fn sign(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> Result<Response, ApiError> {
    guard!(state, headers);
    let (keys, _) = state
        .signer
        .keys_via(&vine_id, opt_text(&body, "connection").as_deref())?;
    let template = body.get("event").cloned().unwrap_or(Value::Null);
    let event = state.signer.sign(&keys, &template)?;
    Ok(Json(json!({"event": event})).into_response())
}

async fn crypt(
    state: AppState,
    headers: HeaderMap,
    vine_id: String,
    body: Value,
    encrypt: bool,
) -> ApiResult {
    guard!(state, headers);
    let (keys, _) = state
        .signer
        .keys_via(&vine_id, opt_text(&body, "connection").as_deref())?;
    let scheme = Scheme::parse(&opt_text(&body, "scheme").unwrap_or_else(|| "nip44".into()))?;
    let (peer, input) = (text(&body, "peer")?, text(&body, "text")?);
    let result = if encrypt {
        state.signer.encrypt(&keys, scheme, &peer, &input)?
    } else {
        state.signer.decrypt(&keys, scheme, &peer, &input)?
    };
    Ok(Json(json!({"result": result})).into_response())
}

type ApiResult = Result<Response, ApiError>;

async fn encrypt(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult {
    crypt(state, headers, vine_id, body, true).await
}

async fn decrypt(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult {
    crypt(state, headers, vine_id, body, false).await
}

async fn derive(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult {
    guard!(state, headers);
    let (keys, conn) = state
        .signer
        .keys_via(&vine_id, opt_text(&body, "connection").as_deref())?;
    let purpose = text(&body, "purpose")?;
    // through an app's connection only the purposes that app was given
    if conn.is_some_and(|c| !c.derive.contains(&purpose)) {
        return Err(Error::Forbidden(format!("this app may not derive {purpose:?}")).into());
    }
    let secret = state.signer.derive(&keys, &purpose)?;
    Ok(Json(json!({"secret": secret})).into_response())
}

async fn export(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
) -> ApiResult {
    guard!(state, headers);
    Ok(Json(json!({"nsec": state.signer.export(&vine_id)?})).into_response())
}

#[derive(Deserialize)]
struct NewConnectionBody {
    app: String,
    #[serde(default)]
    label: String,
    #[serde(default)]
    derive: Vec<String>,
    #[serde(default)]
    keep: Option<usize>,
    /// The NIP-46 client key the connection is for (bound from the start).
    #[serde(default)]
    client_pubkey: Option<String>,
}

async fn create_connection(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<NewConnectionBody>,
) -> ApiResult {
    guard!(state, headers);
    let label = if body.label.is_empty() {
        body.app.clone()
    } else {
        body.label
    };
    let (created, change) = state.signer.create_connection(
        &vine_id,
        &body.app,
        &label,
        &body.derive,
        body.keep,
        body.client_pubkey.as_deref().filter(|c| !c.is_empty()),
    )?;
    // the client may connect the moment it has the URL: the bunker listens for it first
    state
        .signer
        .wait_subscribed(change, Duration::from_millis(1500))
        .await;
    Ok(Json(serde_json::to_value(created).unwrap_or(Value::Null)).into_response())
}

async fn list_connections(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
) -> ApiResult {
    guard!(state, headers);
    let rows = state.signer.connections(&vine_id)?;
    Ok(Json(serde_json::to_value(rows).unwrap_or(Value::Null)).into_response())
}

async fn revoke_connections(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult {
    guard!(state, headers);
    let id = opt_text(&body, "id");
    let bunker = opt_text(&body, "bunker_pubkey");
    let app = opt_text(&body, "app");
    let n = state
        .signer
        .revoke(&vine_id, id.as_deref(), bunker.as_deref(), app.as_deref())?;
    Ok(Json(json!({"revoked": n})).into_response())
}

async fn import(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> ApiResult {
    guard!(state, headers);
    let rows = body
        .get("accounts")
        .and_then(Value::as_array)
        .ok_or_else(|| Error::Invalid("accounts must be a list".into()))?;
    let mut results = Vec::new();
    for row in rows {
        let vine_id = text(row, "vine_id")?;
        let result = (|| -> Result<&'static str, Error> {
            let pubkey = row
                .get("pubkey")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            let sealed = hex::decode(row.get("sealed").and_then(Value::as_str).unwrap_or(""))
                .map_err(|_| Error::Invalid("sealed must be hex".into()))?;
            state.signer.import_sealed(&vine_id, pubkey, &sealed)
        })();
        results.push(match result {
            Ok(r) => json!({"vine_id": vine_id, "result": r}),
            Err(e) => json!({"vine_id": vine_id, "result": "error", "error": e.code(), "message": e.message()}),
        });
    }
    Ok(Json(json!({"results": results})).into_response())
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/import", post(import))
        .route("/accounts/{vine_id}", put(put_account).get(get_account))
        .route("/accounts/{vine_id}/status", post(set_status))
        .route("/accounts/{vine_id}/own-key", post(own_key))
        .route("/accounts/{vine_id}/username", put(set_username))
        .route("/names/{username}", get(name))
        .route("/pubkeys/{pubkey}/status", post(status_by_pubkey))
        .route("/accounts/{vine_id}/sign", post(sign))
        .route("/accounts/{vine_id}/encrypt", post(encrypt))
        .route("/accounts/{vine_id}/decrypt", post(decrypt))
        .route("/accounts/{vine_id}/derive", post(derive))
        .route("/accounts/{vine_id}/export", post(export))
        .route(
            "/accounts/{vine_id}/connections",
            post(create_connection).get(list_connections),
        )
        .route(
            "/accounts/{vine_id}/connections/revoke",
            post(revoke_connections),
        )
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::signer::tests::{signer_with_seed, tempdir};
    use axum::body::Body;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    async fn call(
        app: &Router,
        method: &str,
        path: &str,
        token: &str,
        body: Value,
    ) -> (u16, Value) {
        let req = Request::builder()
            .method(method)
            .uri(path)
            .header("content-type", "application/json")
            .header("authorization", format!("Bearer {token}"))
            .body(Body::from(body.to_string()))
            .unwrap();
        let resp = app.clone().oneshot(req).await.unwrap();
        let status = resp.status().as_u16();
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }

    #[tokio::test]
    async fn the_api_needs_the_token_and_signs_for_an_account() {
        let dir = tempdir("api");
        let app = router(AppState {
            signer: Arc::new(signer_with_seed(&dir)),
            token: Arc::new("t0ken".into()),
        });
        let (s, _) = call(&app, "PUT", "/accounts/odoo-partner-1", "wrong", json!({})).await;
        assert_eq!(s, 401);
        let (s, a) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-1",
            "t0ken",
            json!({"key_version": 0}),
        )
        .await;
        assert_eq!(s, 200, "{a}");
        assert_eq!(a["custody"], "derived");
        let (s, e) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/sign",
            "t0ken",
            json!({"event": {"kind": 0, "content": "{}", "tags": []}}),
        )
        .await;
        assert_eq!(s, 200);
        assert_eq!(e["event"]["pubkey"], a["pubkey"]);
        let (s, d) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/derive",
            "t0ken",
            json!({"purpose": "cashu-wallet-v1"}),
        )
        .await;
        assert_eq!((s, d["secret"].as_str().unwrap().len()), (200, 64));
        let (s, c) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            json!({"app": "belfry-chat"}),
        )
        .await;
        assert_eq!(s, 200);
        assert!(c["bunker_url"].as_str().unwrap().starts_with("bunker://"));
        let (_, list) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!(list.as_array().unwrap().len(), 1);
        assert!(
            list[0].get("secret").is_none(),
            "the secret never leaves the signer again"
        );
        let cid = c["id"].as_str().unwrap().to_string();
        let (s, _) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/derive",
            "t0ken",
            json!({"purpose": "cashu-wallet-v1", "connection": cid}),
        )
        .await;
        assert_eq!(s, 403, "the chat may not derive the wallet's secret");
        let (s, _) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/sign",
            "t0ken",
            json!({"event": {"kind": 1}, "connection": cid}),
        )
        .await;
        assert_eq!(s, 200);
        let (_, r) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections/revoke",
            "t0ken",
            json!({"app": "belfry-chat"}),
        )
        .await;
        assert_eq!(r["revoked"], 1);
        let (s, _) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/sign",
            "t0ken",
            json!({"event": {"kind": 1}, "connection": cid}),
        )
        .await;
        assert_eq!(s, 403, "a revoked connection's token signs nothing");
        let (s, _) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/status",
            "t0ken",
            json!({"status": "suspended"}),
        )
        .await;
        assert_eq!(s, 200);
        let (s, e) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/export",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (403, Some("suspended")));
        let (s, _) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-9",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!(s, 404);
    }

    fn app(name: &str) -> (Router, Arc<Signer>) {
        let signer = Arc::new(signer_with_seed(&tempdir(name)));
        let app = router(AppState {
            signer: signer.clone(),
            token: Arc::new("t0ken".into()),
        });
        (app, signer)
    }

    #[tokio::test]
    async fn health_says_how_the_bunker_is_and_nothing_about_who_is_here() {
        let (app, signer) = app("health");
        call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        let (s, h) = call(&app, "GET", "/health", "", Value::Null).await;
        assert_eq!(s, 200);
        assert_eq!(h["status"], "ok");
        assert_eq!(h["bunker"]["state"], "off");
        assert!(h.get("accounts").is_none(), "{h}");
        signer.bunker.enabled.store(true, Ordering::SeqCst);
        let (_, h) = call(&app, "GET", "/health", "", Value::Null).await;
        assert_eq!(
            (h["status"].as_str(), h["bunker"]["state"].as_str()),
            (Some("degraded"), Some("disconnected"))
        );
        assert_eq!(h["bunker"]["last_event_at"], Value::Null);
        signer.bunker.connected.store(true, Ordering::SeqCst);
        signer
            .bunker
            .last_event_at
            .store(1_700_000_000, Ordering::SeqCst);
        let (_, h) = call(&app, "GET", "/health", "", Value::Null).await;
        assert_eq!(
            (h["status"].as_str(), h["bunker"]["state"].as_str()),
            (Some("ok"), Some("connected"))
        );
        assert_eq!(h["bunker"]["last_event_at"], 1_700_000_000);
    }

    #[tokio::test]
    async fn sessions_are_made_for_a_client_listed_and_revoked_by_their_bunker_key() {
        let (app, _) = app("sessions");
        for id in ["odoo-partner-1", "guest-2"] {
            call(&app, "PUT", &format!("/accounts/{id}"), "t0ken", json!({})).await;
        }
        let client = nostr::prelude::Keys::generate().public_key().to_hex();
        let (s, c) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            json!({"app": "Chat · Firefox on Linux", "client_pubkey": client}),
        )
        .await;
        assert_eq!(s, 200, "{c}");
        assert_eq!(c["client_pubkey"], client);
        assert_eq!(c["app"], "Chat · Firefox on Linux");
        assert!(c["created_at"].as_i64().unwrap() > 0);
        let (s, e) = call(
            &app,
            "POST",
            "/accounts/guest-2/connections",
            "t0ken",
            json!({"app": "Chat", "client_pubkey": client}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (409, Some("client_key_taken")));
        let (s, e) = call(
            &app,
            "POST",
            "/accounts/guest-2/connections",
            "t0ken",
            json!({"app": "Chat", "client_pubkey": "zz"}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (422, Some("invalid")));
        let (_, list) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            Value::Null,
        )
        .await;
        let row = &list[0];
        assert_eq!(row["bunker_pubkey"], c["bunker_pubkey"]);
        assert_eq!(row["client_pubkey"], client);
        assert_eq!(row["app"], "Chat · Firefox on Linux");
        assert_eq!(
            (row["uses"].as_i64(), row["last_used_at"].clone()),
            (Some(0), Value::Null)
        );
        assert!(row["created_at"].as_i64().unwrap() > 0);
        for hidden in ["secret", "secret_hash", "bunker_sealed"] {
            assert!(row.get(hidden).is_none(), "{hidden}");
        }
        // another account cannot revoke it; its own does, once
        let body = json!({"bunker_pubkey": c["bunker_pubkey"]});
        let (_, r) = call(
            &app,
            "POST",
            "/accounts/guest-2/connections/revoke",
            "t0ken",
            body.clone(),
        )
        .await;
        assert_eq!(r["revoked"], 0);
        let (_, r) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections/revoke",
            "t0ken",
            body.clone(),
        )
        .await;
        assert_eq!(r["revoked"], 1);
        let (_, r) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections/revoke",
            "t0ken",
            body,
        )
        .await;
        assert_eq!(r["revoked"], 0);
        let (_, list) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            Value::Null,
        )
        .await;
        assert!(list.as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn usernames_statuses_by_key_and_the_move_to_an_own_key() {
        let (app, _) = app("names");
        let (s, a) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-1",
            "t0ken",
            json!({"username": "0001"}),
        )
        .await;
        assert_eq!((s, a["username"].as_str()), (200, Some("0001")));
        let (s, e) = call(
            &app,
            "PUT",
            "/accounts/guest-2",
            "t0ken",
            json!({"username": "0001"}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (409, Some("username_taken")));
        let (s, _) = call(&app, "GET", "/accounts/guest-2", "t0ken", Value::Null).await;
        assert_eq!(s, 404, "nothing was created");
        call(
            &app,
            "PUT",
            "/accounts/guest-2",
            "t0ken",
            json!({"username": "guest-2"}),
        )
        .await;
        let (s, n) = call(
            &app,
            "PUT",
            "/accounts/guest-2/username",
            "t0ken",
            json!({"username": "Lina"}),
        )
        .await;
        assert_eq!((s, n["username"].as_str()), (200, Some("lina")));
        let (s, e) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-1/username",
            "t0ken",
            json!({"username": "LINA"}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (409, Some("username_taken")));
        let (s, e) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-1/username",
            "t0ken",
            json!({"username": "no spaces"}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (422, Some("invalid")));
        let (s, n) = call(&app, "GET", "/names/LINA", "t0ken", Value::Null).await;
        assert_eq!(s, 200);
        assert_eq!(
            (n["name"].as_str(), n["vine_id"].as_str()),
            (Some("lina"), Some("guest-2"))
        );
        let (s, _) = call(&app, "GET", "/names/guest-2", "t0ken", Value::Null).await;
        assert_eq!(s, 404, "the old name is free");
        let (s, _) = call(&app, "GET", "/names/lina", "wrong", Value::Null).await;
        assert_eq!(s, 401);

        let pk = a["pubkey"].as_str().unwrap();
        let (s, r) = call(
            &app,
            "POST",
            &format!("/pubkeys/{pk}/status"),
            "t0ken",
            json!({"status": "suspended"}),
        )
        .await;
        assert_eq!((s, r["updated"].as_i64()), (200, Some(1)));
        let (_, a2) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!(a2["status"], "suspended");
        let other = nostr::prelude::Keys::generate().public_key().to_hex();
        let (s, _) = call(
            &app,
            "POST",
            &format!("/pubkeys/{other}/status"),
            "t0ken",
            json!({"status": "active"}),
        )
        .await;
        assert_eq!(s, 404);

        // a key the caller expects, and one it does not
        let (s, _) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-1",
            "t0ken",
            json!({"expect_pubkey": pk}),
        )
        .await;
        assert_eq!(s, 200);
        for body in [
            json!({"expect_pubkey": other}),
            json!({"own_pubkey": other}),
        ] {
            let (s, e) = call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", body).await;
            assert_eq!((s, e["error"].as_str()), (409, Some("mismatch")));
        }
        let (_, same) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!(
            (same["pubkey"].as_str(), same["custody"].as_str()),
            (Some(pk), Some("derived"))
        );
        // the move is its own call
        let (s, moved) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/own-key",
            "t0ken",
            json!({"pubkey": other}),
        )
        .await;
        assert_eq!(s, 200);
        assert_eq!(
            (moved["pubkey"].as_str(), moved["custody"].as_str()),
            (Some(other.as_str()), Some("own"))
        );
        assert_eq!(moved["username"], "0001", "the name stays");

        // an import that names another key for an account that exists is an error
        let (s, r) = call(
            &app,
            "POST",
            "/import",
            "t0ken",
            json!({"accounts": [
            {"vine_id": "guest-2", "pubkey": other, "sealed": "00"}]}),
        )
        .await;
        assert_eq!(s, 200);
        assert_eq!(
            (
                r["results"][0]["result"].as_str(),
                r["results"][0]["error"].as_str()
            ),
            (Some("error"), Some("mismatch"))
        );
    }
}
