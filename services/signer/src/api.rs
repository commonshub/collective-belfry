//! The internal HTTP API. Only the onboarding service calls it (Compose network `signer-api`,
//! which the bunker relay is not on; no published port), with the bearer token from
//! `/run/secrets/signer_token`.
//!
//! ```text
//! GET  /health                                  {status, seed, bunker}     (no token)
//! GET  /settings                                                           -> {no_new_accounts, by_environment}
//! POST /settings                                {no_new_accounts: bool}    -> the same
//! GET  /accounts?status=&after=&limit=                                     -> {accounts, next, total, suspended}
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
//! POST /accounts/{vine_id}/connections          {app, label?, client_pubkey?, derive?, keep?, scope?,
//!                                                decrypt?, origin?, kind?}
//! GET  /accounts/{vine_id}/connections[?all=true]                          -> [{id, app, ...}]
//! POST /accounts/{vine_id}/connections/revoke   {id | bunker_pubkey | app | all: true} -> {revoked}
//! POST /accounts/{vine_id}/connections/consent  {id | bunker_pubkey, decrypt: bool} -> {decrypt}
//! GET  /names/{username}                                                   -> {name, pubkey, vine_id}
//! POST /pubkeys/{pubkey}/status                 {status}                   -> {updated}
//! POST /import                                  {accounts: [{vine_id, pubkey, sealed}]}
//! POST /import/connections                      {vine_id, connections: [{bunker_pubkey,
//!                                                keycast_secret_hash, client_pubkey, app, label?,
//!                                                scope?, decrypt?, origin?, created_at,
//!                                                last_used_at?, uses?, revoked_at?}]}
//!                                                                          -> {imported, existing, revoked}
//! ```
//!
//! sign, encrypt, decrypt and derive take an optional `connection` (the id behind an app's
//! access token): it must be live, derive allows only that connection's purposes, and through a
//! connection of scope `outside` sign keeps to the limits of `limits.rs` and decrypt needs the
//! connection's consent flag (403).
//!
//! A connection of `kind: "http"` is an app's sign-in that is used through this API: it stays
//! when no NIP-46 client connects to it (only its secret ends at the deadline).
//!
//! `/import/connections` carries one account's Keycast sessions over, all or none
//! (`Signer::import_connections`): at most 1000 rows and 1 MB a call (413 `too_large`).
//!
//! Errors are `{"error": <code>, "message"}` with 404 not_found, 503 seed_locked /
//! bunker_unavailable, 409 own_key / mismatch / client_key_taken / username_taken /
//! no_new_accounts, 403 suspended / forbidden, 413 too_large, 422 invalid, 401 unauthorized.

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::rejection::JsonRejection;
use axum::extract::{DefaultBodyLimit, Path, RawQuery, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use subtle::ConstantTimeEq;

use crate::limits;
use crate::signer::{ConnectionSpec, Error, KeycastSession, Scheme, Signer};
use crate::store::SCOPE_OUTSIDE;

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
            Error::SeedLocked | Error::BunkerUnavailable => StatusCode::SERVICE_UNAVAILABLE,
            Error::OwnKey
            | Error::Mismatch(_)
            | Error::ClientKeyTaken
            | Error::UsernameTaken
            | Error::NoNewAccounts => StatusCode::CONFLICT,
            Error::TooLarge(_) => StatusCode::PAYLOAD_TOO_LARGE,
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

/// One value of the query string (`key=value&…`; the values used here need no decoding).
fn query_value(query: &Option<String>, key: &str) -> Option<String> {
    query.as_deref()?.split('&').find_map(|pair| {
        let (k, v) = pair.split_once('=')?;
        (k == key && !v.is_empty()).then(|| v.to_string())
    })
}

fn settings_json(signer: &Signer) -> Result<Value, Error> {
    Ok(json!({
        "no_new_accounts": signer.accounts_closed()?,
        "by_environment": signer.no_new_accounts_env,
    }))
}

async fn get_settings(State(state): State<AppState>, headers: HeaderMap) -> ApiResult {
    guard!(state, headers);
    Ok(Json(settings_json(&state.signer)?).into_response())
}

async fn post_settings(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> ApiResult {
    guard!(state, headers);
    let closed = body
        .get("no_new_accounts")
        .and_then(Value::as_bool)
        .ok_or_else(|| Error::Invalid("no_new_accounts must be true or false".into()))?;
    state.signer.close_accounts(closed)?;
    Ok(Json(settings_json(&state.signer)?).into_response())
}

/// The most accounts one page of the list carries.
const MAX_ACCOUNTS_PAGE: usize = 1000;

/// Every account, or those with `status`, in the order of their ids: a page after `after`,
/// and `next` (the id to ask after) while there is more. `total` and `suspended` count all.
async fn list_accounts(
    State(state): State<AppState>,
    headers: HeaderMap,
    RawQuery(query): RawQuery,
) -> ApiResult {
    guard!(state, headers);
    let status = query_value(&query, "status").filter(|s| s != "all");
    let after = query_value(&query, "after").unwrap_or_default();
    let limit = match query_value(&query, "limit") {
        None => 500,
        Some(n) => n
            .parse::<usize>()
            .ok()
            .filter(|n| (1..=MAX_ACCOUNTS_PAGE).contains(n))
            .ok_or_else(|| Error::Invalid(format!("limit must be 1 to {MAX_ACCOUNTS_PAGE}")))?,
    };
    let (rows, (total, suspended)) =
        state
            .signer
            .list_accounts(status.as_deref(), &after, limit)?;
    let next = (rows.len() == limit)
        .then(|| rows.last().map(|a| a.vine_id.clone()))
        .flatten();
    Ok(Json(json!({
        "accounts": rows.iter().map(account_json).collect::<Vec<_>>(),
        "next": next,
        "total": total,
        "suspended": suspended,
    }))
    .into_response())
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
    let (keys, conn) = state
        .signer
        .keys_via(&vine_id, opt_text(&body, "connection").as_deref())?;
    let template = body.get("event").cloned().unwrap_or(Value::Null);
    // through an outside app's connection: the same limits as over the bunker
    if conn.is_some_and(|c| c.scope == SCOPE_OUTSIDE)
        && limits::forbidden(&template, &state.signer.own)
    {
        return Err(Error::Forbidden(limits::REFUSAL.into()).into());
    }
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
    let (keys, conn) = state
        .signer
        .keys_via(&vine_id, opt_text(&body, "connection").as_deref())?;
    // through an outside app's connection: reading needs the member's consent, as over the
    // bunker
    if !encrypt && conn.as_ref().is_some_and(Signer::decrypt_refused) {
        return Err(Error::Forbidden(limits::REFUSAL.into()).into());
    }
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
    /// `own` (the default) or `outside`: an outside app, held to the signing limits.
    #[serde(default)]
    scope: Option<Value>,
    /// The member's consent to decrypt, for an outside app (default false). Anything but
    /// true or false is refused by the body's parser, never read as a yes.
    #[serde(default)]
    decrypt: bool,
    /// The address the session is opened from (the app's registered address).
    #[serde(default)]
    origin: Option<String>,
    /// `nip46` (the default) or `http`: an app's sign-in that is used through the API.
    #[serde(default)]
    kind: Option<String>,
}

/// How long the API waits for the bunker's subscription to cover a connection it just made.
const SUBSCRIBE_WAIT: Duration = Duration::from_millis(1500);

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
    // a scope that is not text is refused like an unknown one, never read as "own"
    let scope = match &body.scope {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.as_str()),
        Some(_) => {
            return Err(Error::Invalid("scope must be \"own\" or \"outside\"".into()).into())
        }
    };
    let (created, change) = state.signer.create_scoped(
        &vine_id,
        &ConnectionSpec {
            app: &body.app,
            label: &label,
            derive: &body.derive,
            keep: body.keep,
            client_pubkey: body.client_pubkey.as_deref().filter(|c| !c.is_empty()),
            scope,
            decrypt: body.decrypt,
            origin: body.origin.as_deref().unwrap_or(""),
            kind: body.kind.as_deref(),
        },
    )?;
    // the client may connect the moment it has the URL: the bunker listens for it first.
    // When it does not (the relay is away, the subscription failed), the URL is not handed
    // out: the connection is taken back and the caller asks again. A sign-in over HTTP stands
    // without the bunker, so it is answered all the same.
    if let Err(e) = state.signer.wait_subscribed(change, SUBSCRIBE_WAIT).await {
        if created.kind != crate::store::KIND_HTTP {
            let _ = state.signer.revoke(&vine_id, Some(&created.id), None, None);
            tracing::warn!(vine_id, id = %created.id, "connection taken back: the bunker is not listening");
            return Err(e.into());
        }
    }
    Ok(Json(serde_json::to_value(created).unwrap_or(Value::Null)).into_response())
}

/// The account's live connections; with `?all=true` every one it ever had (`revoked_at` set
/// on the revoked ones), for the migration's verify.
async fn list_connections(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    RawQuery(query): RawQuery,
) -> ApiResult {
    guard!(state, headers);
    let rows = match query_value(&query, "all").as_deref() {
        None | Some("false") | Some("0") => state.signer.connections(&vine_id)?,
        Some("true") | Some("1") => state.signer.all_connections(&vine_id)?,
        Some(_) => return Err(Error::Invalid("all must be true or false".into()).into()),
    };
    Ok(Json(serde_json::to_value(rows).unwrap_or(Value::Null)).into_response())
}

/// The member's consent to decrypt, on one live connection: the sign-in page (or a settings
/// page) asks, the service tells the signer.
async fn consent(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(vine_id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult {
    guard!(state, headers);
    let decrypt = body
        .get("decrypt")
        .and_then(Value::as_bool)
        .ok_or_else(|| Error::Invalid("decrypt must be true or false".into()))?;
    state.signer.set_decrypt(
        &vine_id,
        opt_text(&body, "id").as_deref(),
        opt_text(&body, "bunker_pubkey").as_deref(),
        decrypt,
    )?;
    Ok(Json(json!({"decrypt": decrypt})).into_response())
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
    // an empty or missing selector never means "all of them"
    let all = body.get("all").and_then(Value::as_bool) == Some(true);
    if id.is_none() && bunker.is_none() && app.is_none() && !all {
        return Err(
            Error::Invalid("say which: id, bunker_pubkey, app, or all: true".into()).into(),
        );
    }
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

/// The most sessions one call carries (one account's), and the largest body of that call.
/// The migration sends 400 rows or 256 kB a call (`migration.py`), so both leave room; a row
/// is about 400 bytes.
const MAX_IMPORTED_CONNECTIONS: usize = 1000;
const MAX_IMPORT_BODY: usize = 1024 * 1024;

fn session_of(i: usize, row: &Value) -> Result<KeycastSession, Error> {
    let bad = |what: &str| Error::Invalid(format!("connections[{i}]: {what}"));
    if !row.is_object() {
        return Err(bad("must be an object"));
    }
    let need = |key: &str| {
        row.get(key)
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| bad(&format!("{key} must be text")))
    };
    // missing or null: None; anything else must be of the right type
    let opt_str = |key: &str| match row.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(bad(&format!("{key} must be text or null"))),
    };
    let opt_bool = |key: &str| match row.get(key) {
        None | Some(Value::Null) => Ok(false),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => Err(bad(&format!("{key} must be true or false"))),
    };
    let opt_int = |key: &str| match row.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(v) => v
            .as_i64()
            .map(Some)
            .ok_or_else(|| bad(&format!("{key} must be a whole number or null"))),
    };
    Ok(KeycastSession {
        bunker_pubkey: need("bunker_pubkey")?,
        keycast_secret_hash: need("keycast_secret_hash")?,
        client_pubkey: opt_str("client_pubkey")?,
        app: need("app")?,
        label: opt_str("label")?.unwrap_or_default(),
        scope: opt_str("scope")?,
        decrypt: opt_bool("decrypt")?,
        origin: opt_str("origin")?.unwrap_or_default(),
        created_at: opt_int("created_at")?
            .ok_or_else(|| bad("created_at must be a whole number"))?,
        last_used_at: opt_int("last_used_at")?,
        uses: opt_int("uses")?.unwrap_or(0),
        revoked_at: opt_int("revoked_at")?,
    })
}

async fn import_connections(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<Value>, JsonRejection>,
) -> ApiResult {
    guard!(state, headers);
    let Json(body) = body.map_err(|rejection| {
        if rejection.status() == StatusCode::PAYLOAD_TOO_LARGE {
            Error::TooLarge(format!(
                "at most {MAX_IMPORT_BODY} bytes and {MAX_IMPORTED_CONNECTIONS} connections a \
                 call: send this account's sessions in smaller batches"
            ))
        } else {
            Error::Invalid(rejection.body_text())
        }
    })?;
    let vine_id = text(&body, "vine_id")?;
    let rows = body
        .get("connections")
        .and_then(Value::as_array)
        .ok_or_else(|| Error::Invalid("connections must be a list".into()))?;
    if rows.len() > MAX_IMPORTED_CONNECTIONS {
        return Err(Error::Invalid(format!(
            "at most {MAX_IMPORTED_CONNECTIONS} connections a call"
        ))
        .into());
    }
    let sessions = rows
        .iter()
        .enumerate()
        .map(|(i, row)| session_of(i, row))
        .collect::<Result<Vec<_>, _>>()?;
    // deriving and sealing a few thousand keys is not for the async threads
    let signer = state.signer.clone();
    let (counts, change) =
        tokio::task::spawn_blocking(move || signer.import_connections(&vine_id, &sessions))
            .await
            .map_err(|e| Error::Internal(format!("import: {e}")))??;
    // the carried sessions' apps are running: the bunker listens for them before the answer.
    // The rows are written either way (asking again says "existing"): a bunker that is not
    // listening shows in /health, not here.
    let _ = state.signer.wait_subscribed(change, SUBSCRIBE_WAIT).await;
    Ok(Json(serde_json::to_value(counts).unwrap_or(Value::Null)).into_response())
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/settings", get(get_settings).post(post_settings))
        .route("/import", post(import))
        .route(
            "/import/connections",
            post(import_connections).layer(DefaultBodyLimit::max(MAX_IMPORT_BODY)),
        )
        .route("/accounts", get(list_accounts))
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
        .route("/accounts/{vine_id}/connections/consent", post(consent))
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
        // a long name with wide characters; and "all" has to be said
        let name = "é".repeat(60);
        for _ in 0..2 {
            let (s, c) = call(
                &app,
                "POST",
                "/accounts/guest-2/connections",
                "t0ken",
                json!({"app": name}),
            )
            .await;
            assert_eq!((s, c["app"].as_str()), (200, Some(name.as_str())));
        }
        for body in [json!({}), json!({"bunker_pubkey": ""}), json!({"id": ""})] {
            let (s, e) = call(
                &app,
                "POST",
                "/accounts/guest-2/connections/revoke",
                "t0ken",
                body,
            )
            .await;
            assert_eq!((s, e["error"].as_str()), (422, Some("invalid")));
        }
        let (_, r) = call(
            &app,
            "POST",
            "/accounts/guest-2/connections/revoke",
            "t0ken",
            json!({"all": true}),
        )
        .await;
        assert_eq!(r["revoked"], 2);
    }

    /// One NIP-46 request of `client` to the connection `bunker`, answered by the bunker's
    /// own code (no relay in between).
    fn nip46(
        signer: &Signer,
        bunker: &nostr::prelude::Keys,
        client: &nostr::prelude::Keys,
        method: &str,
        params: Value,
    ) -> Option<Value> {
        use nostr::nips::nip44;
        use nostr::prelude::{EventBuilder, FinalizeEvent, Kind, Tag};
        let body = json!({"id": "1", "method": method, "params": params});
        let content = nip44::encrypt(
            client.secret_key(),
            &bunker.public_key(),
            body.to_string(),
            nip44::Version::V2,
        )
        .unwrap();
        let request = EventBuilder::new(Kind::NostrConnect, content)
            .tag(Tag::public_key(bunker.public_key()))
            .finalize(client)
            .unwrap();
        let answer = crate::bunker::respond(signer, &request)?;
        assert_eq!(answer.pubkey, bunker.public_key());
        let plain =
            nip44::decrypt(client.secret_key(), &bunker.public_key(), &answer.content).unwrap();
        Some(serde_json::from_str(&plain).unwrap())
    }

    #[tokio::test]
    async fn keycast_sessions_are_carried_over_with_their_bunker_and_client_keys() {
        use crate::keys;
        use crate::store::{Imported, ImportedConnection};
        use nostr::prelude::{Event, Keys};
        let (app, signer) = app("carry");
        for id in ["odoo-partner-1", "guest-2"] {
            call(&app, "PUT", &format!("/accounts/{id}"), "t0ken", json!({})).await;
        }
        // what Keycast had: the account's key, and per session a stored secret_hash from
        // which its bunker key derives
        let account = keys::derive_keys(&[7u8; 32], "odoo-partner-1", 0);
        let hash = |n: u32| format!("$2b$10$N9qo8uLOickgx2ZMRZoMye{n:031}");
        let bunker = |n: u32| keys::keycast_bunker_keys(&account, &hash(n));
        let (phone, laptop, old) = (Keys::generate(), Keys::generate(), Keys::generate());
        let row = |n: u32, client: Option<&Keys>| {
            json!({
                "bunker_pubkey": bunker(n).public_key().to_hex(),
                "keycast_secret_hash": hash(n),
                "client_pubkey": client.map(|c| c.public_key().to_hex()),
                "app": "Chat · Firefox on Linux",
                "label": "",
                "created_at": 1_700_000_000,
                "last_used_at": 1_700_000_500,
                "uses": 7,
                "revoked_at": null,
            })
        };
        let revoked = |n: u32, client: Option<&Keys>| {
            let mut r = row(n, client);
            r["revoked_at"] = json!(1_700_001_000);
            r
        };
        let import = |vine_id: &'static str, rows: Value| {
            let app = app.clone();
            async move {
                call(
                    &app,
                    "POST",
                    "/import/connections",
                    "t0ken",
                    json!({"vine_id": vine_id, "connections": rows}),
                )
                .await
            }
        };
        let listed = || async {
            call(
                &app,
                "GET",
                "/accounts/odoo-partner-1/connections",
                "t0ken",
                Value::Null,
            )
            .await
            .1
        };
        let (s, _) = call(&app, "POST", "/import/connections", "wrong", json!({})).await;
        assert_eq!(s, 401);

        // a bunker key the formula does not give: the row is named, nothing is written
        let mut wrong = row(2, Some(&laptop));
        wrong["keycast_secret_hash"] = json!(hash(9));
        let (s, e) = import("odoo-partner-1", json!([row(1, Some(&phone)), wrong])).await;
        assert_eq!((s, e["error"].as_str()), (422, Some("invalid")), "{e}");
        assert!(e["message"]
            .as_str()
            .unwrap()
            .starts_with("connections[1]:"));
        assert!(listed().await.as_array().unwrap().is_empty());
        // a live session nobody connected to has nothing to carry; odd fields are refused
        let mut bad_scope = row(1, Some(&phone));
        bad_scope["scope"] = json!("all");
        let mut no_time = row(1, Some(&phone));
        no_time["created_at"] = Value::Null;
        let mut bad_client = row(1, Some(&phone));
        bad_client["client_pubkey"] = json!("zz");
        for bad in [row(2, None), bad_scope, no_time, bad_client, json!("row")] {
            let (s, e) = import("odoo-partner-1", json!([row(1, Some(&phone)), bad])).await;
            assert_eq!((s, e["error"].as_str()), (422, Some("invalid")), "{e}");
            assert!(e["message"].as_str().unwrap().starts_with("connections[1]"));
        }
        assert!(listed().await.as_array().unwrap().is_empty());
        assert_eq!(signer.bunker.changes(), 0);

        // two live sessions (one of an outside app) and two revoked ones
        let mut outside = row(2, Some(&laptop));
        outside["scope"] = json!("outside");
        outside["app"] = json!("Notes");
        outside["label"] = json!("Notes on the laptop");
        outside["last_used_at"] = Value::Null;
        let rows = json!([
            row(1, Some(&phone)),
            outside,
            revoked(3, Some(&old)),
            revoked(4, None)
        ]);
        let (s, r) = import("odoo-partner-1", rows.clone()).await;
        assert_eq!(s, 200, "{r}");
        assert_eq!(r, json!({"imported": 2, "existing": 0, "revoked": 2}));
        assert_eq!(signer.bunker.changes(), 1, "the bunker subscribes again");
        let mut live = vec![
            bunker(1).public_key().to_hex(),
            bunker(2).public_key().to_hex(),
        ];
        live.sort();
        assert_eq!(signer.store.live_bunker_pubkeys().unwrap(), live);
        let list = listed().await;
        assert_eq!(list.as_array().unwrap().len(), 2);
        let of = |n: u32| {
            let key = bunker(n).public_key().to_hex();
            list.as_array()
                .unwrap()
                .iter()
                .find(|c| c["bunker_pubkey"] == key.as_str())
                .unwrap()
                .clone()
        };
        let first = of(1);
        assert_eq!(first["client_pubkey"], phone.public_key().to_hex());
        assert_eq!(first["app"], "Chat · Firefox on Linux");
        assert_eq!(first["label"], "Chat · Firefox on Linux");
        assert_eq!(first["scope"], "own");
        assert_eq!(
            (
                first["created_at"].as_i64(),
                first["last_used_at"].as_i64(),
                first["uses"].as_i64(),
                first["expires_at"].clone()
            ),
            (
                Some(1_700_000_000),
                Some(1_700_000_500),
                Some(7),
                Value::Null
            )
        );
        let second = of(2);
        assert_eq!(
            (
                second["scope"].as_str(),
                second["label"].as_str(),
                second["last_used_at"].clone()
            ),
            (Some("outside"), Some("Notes on the laptop"), Value::Null)
        );

        // the phone goes on as if nothing happened: no connect, the same bunker key, its key
        let r = nip46(&signer, &bunker(1), &phone, "get_public_key", json!([])).unwrap();
        assert_eq!(r["result"], account.public_key().to_hex());
        let template = json!({"kind": 9, "content": "still here", "tags": [["h", "general"]]});
        let r = nip46(
            &signer,
            &bunker(1),
            &phone,
            "sign_event",
            json!([template.to_string()]),
        )
        .unwrap();
        let ev = Event::from_json(r["result"].as_str().unwrap()).unwrap();
        ev.verify().unwrap();
        assert_eq!(ev.pubkey, account.public_key());
        let r = nip46(&signer, &bunker(1), &phone, "connect", json!([])).unwrap();
        assert_eq!(r["result"], "ack");
        // nobody else gets in on it (no secret was carried), and its scope came along
        let r = nip46(&signer, &bunker(1), &laptop, "connect", json!(["", "x"])).unwrap();
        assert!(r["error"].as_str().unwrap().starts_with("unauthorized"));
        let profile = json!({"kind": 0, "content": "{}", "tags": []});
        let r = nip46(
            &signer,
            &bunker(2),
            &laptop,
            "sign_event",
            json!([profile.to_string()]),
        )
        .unwrap();
        assert_eq!(r["error"], "not allowed for this app");
        // a revoked session stays silent
        assert!(nip46(&signer, &bunker(3), &old, "ping", json!([])).is_none());

        // again: nothing changes, whatever the rows now say
        let mut again = rows.clone();
        again[0]["app"] = json!("Another name");
        again[0]["revoked_at"] = json!(1_700_002_000);
        let (s, r) = import("odoo-partner-1", again).await;
        assert_eq!(s, 200);
        assert_eq!(r, json!({"imported": 0, "existing": 4, "revoked": 0}));
        assert_eq!(signer.bunker.changes(), 1);
        let list = listed().await;
        assert_eq!(list.as_array().unwrap().len(), 2);
        assert!(list
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["app"] != "Another name"));
        // one new row among known ones
        let (_, r) = import(
            "odoo-partner-1",
            json!([row(1, Some(&phone)), row(5, Some(&phone))]),
        )
        .await;
        assert_eq!(r, json!({"imported": 1, "existing": 1, "revoked": 0}));

        // one client key, one identity: the revoked session's key stays the first account's
        let (s, e) = call(
            &app,
            "POST",
            "/accounts/guest-2/connections",
            "t0ken",
            json!({"app": "Chat", "client_pubkey": old.public_key().to_hex()}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (409, Some("client_key_taken")));
        // and a session of another account with one of these keys is refused, with its
        // neighbours
        let guest = keys::derive_keys(&[7u8; 32], "guest-2", 0);
        let guest_row = |n: u32, client: &Keys, revoked_at: Value| {
            json!({
                "bunker_pubkey": keys::keycast_bunker_keys(&guest, &hash(n)).public_key().to_hex(),
                "keycast_secret_hash": hash(n),
                "client_pubkey": client.public_key().to_hex(),
                "app": "Chat",
                "created_at": 1_700_000_000,
                "revoked_at": revoked_at,
            })
        };
        let tablet = Keys::generate();
        for taken in [&phone, &old] {
            for revoked_at in [Value::Null, json!(1_700_001_000)] {
                let rows = json!([
                    guest_row(1, &tablet, Value::Null),
                    guest_row(2, taken, revoked_at)
                ]);
                let (s, e) = import("guest-2", rows).await;
                assert_eq!((s, e["error"].as_str()), (409, Some("client_key_taken")));
            }
        }
        let (_, list) = call(
            &app,
            "GET",
            "/accounts/guest-2/connections",
            "t0ken",
            Value::Null,
        )
        .await;
        assert!(list.as_array().unwrap().is_empty(), "nothing was written");
        let (s, r) = import("guest-2", json!([guest_row(1, &tablet, Value::Null)])).await;
        assert_eq!((s, r["imported"].as_i64()), (200, Some(1)));
        // the same bunker key under another account (it cannot be derived there; the store
        // refuses it all the same)
        let stray = ImportedConnection {
            id: "stray".into(),
            app: "Chat".into(),
            label: "Chat".into(),
            bunker_pubkey: bunker(1).public_key().to_hex(),
            bunker_sealed: vec![0],
            client_pubkey: None,
            scope: "own".into(),
            decrypt: false,
            origin: String::new(),
            created_at: 1,
            last_used_at: None,
            uses: 0,
            revoked_at: Some(2),
        };
        assert_eq!(
            signer
                .store
                .import_connections("guest-2", &[stray])
                .unwrap(),
            Imported::OtherAccount(0)
        );
        // an account whose key Keycast held under another account's name derives nothing
        let (s, e) = import("guest-2", json!([row(6, Some(&tablet))])).await;
        assert_eq!((s, e["error"].as_str()), (422, Some("invalid")), "{e}");

        // a suspended account's sessions are carried; an own key has none; unknown: 404
        call(
            &app,
            "POST",
            "/accounts/guest-2/status",
            "t0ken",
            json!({"status": "suspended"}),
        )
        .await;
        let (s, r) = import("guest-2", json!([guest_row(3, &tablet, Value::Null)])).await;
        assert_eq!((s, r["imported"].as_i64()), (200, Some(1)), "{r}");
        let own = Keys::generate().public_key().to_hex();
        call(
            &app,
            "PUT",
            "/accounts/odoo-partner-3",
            "t0ken",
            json!({"own_pubkey": own}),
        )
        .await;
        let (s, e) = import("odoo-partner-3", json!([])).await;
        assert_eq!((s, e["error"].as_str()), (409, Some("own_key")));
        let (s, _) = import("odoo-partner-9", json!([])).await;
        assert_eq!(s, 404);
        let (s, r) = import("odoo-partner-1", json!([])).await;
        assert_eq!(
            (s, r),
            (200, json!({"imported": 0, "existing": 0, "revoked": 0}))
        );
        let (s, e) = call(
            &app,
            "POST",
            "/import/connections",
            "t0ken",
            json!({"vine_id": "odoo-partner-1"}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (422, Some("invalid")));
    }

    #[tokio::test]
    async fn a_connection_has_a_scope_and_an_outside_one_signs_within_the_limits() {
        let (app, _) = app("scope");
        call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        let make = |body: Value| {
            let app = app.clone();
            async move {
                call(
                    &app,
                    "POST",
                    "/accounts/odoo-partner-1/connections",
                    "t0ken",
                    body,
                )
                .await
            }
        };
        for scope in [json!("everything"), json!(""), json!(1), json!(["outside"])] {
            let (s, e) = make(json!({"app": "Notes", "scope": scope})).await;
            assert_eq!((s, e["error"].as_str()), (422, Some("invalid")), "{e}");
        }
        let (s, own) = make(json!({"app": "Chat"})).await;
        assert_eq!((s, own["scope"].as_str()), (200, Some("own")));
        let (s, own2) = make(json!({"app": "Chat", "scope": "own"})).await;
        assert_eq!((s, own2["scope"].as_str()), (200, Some("own")));
        let (s, outside) = make(json!({"app": "Notes", "scope": "outside"})).await;
        assert_eq!((s, outside["scope"].as_str()), (200, Some("outside")));
        let (_, list) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            Value::Null,
        )
        .await;
        let scopes: Vec<&str> = list
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["scope"].as_str().unwrap())
            .collect();
        assert_eq!(scopes, ["outside", "own", "own"]);
        // the API's sign through a connection keeps to the same limits
        let sign = |connection: &Value, kind: u32| {
            let (app, connection) = (app.clone(), connection["id"].clone());
            async move {
                call(
                    &app,
                    "POST",
                    "/accounts/odoo-partner-1/sign",
                    "t0ken",
                    json!({"event": {"kind": kind}, "connection": connection}),
                )
                .await
            }
        };
        let (s, e) = sign(&outside, 9000).await;
        assert_eq!((s, e["error"].as_str()), (403, Some("forbidden")));
        assert_eq!(e["message"], "not allowed for this app");
        assert_eq!(sign(&outside, 1).await.0, 200);
        assert_eq!(sign(&own, 9000).await.0, 200);
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

    #[tokio::test]
    async fn a_connection_carries_consent_an_origin_and_a_kind() {
        let (app, signer) = app("carry-more");
        call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        call(&app, "PUT", "/accounts/guest-2", "t0ken", json!({})).await;
        let make = |body: Value| {
            let app = app.clone();
            async move {
                call(
                    &app,
                    "POST",
                    "/accounts/odoo-partner-1/connections",
                    "t0ken",
                    body,
                )
                .await
            }
        };
        // defaults: no consent, no origin, a NIP-46 connection
        let (s, plain) = make(json!({"app": "Notes", "scope": "outside"})).await;
        assert_eq!(s, 200, "{plain}");
        assert_eq!(
            (
                plain["decrypt"].as_bool(),
                plain["origin"].as_str(),
                plain["kind"].as_str()
            ),
            (Some(false), Some(""), Some("nip46"))
        );
        // consent is true or false, never something that looks like a yes
        for decrypt in [json!("true"), json!(1), json!("yes")] {
            let (s, _) =
                make(json!({"app": "Notes", "scope": "outside", "decrypt": decrypt})).await;
            assert_eq!(s, 422);
        }
        for (key, value) in [
            ("kind", json!("smoke-signals")),
            ("origin", json!("https://x.example/\u{7}")),
            ("origin", json!("x".repeat(201))),
        ] {
            let mut body = json!({"app": "Notes"});
            body[key] = value;
            let (s, e) = make(body).await;
            assert_eq!(
                (s, e["error"].as_str()),
                (422, Some("invalid")),
                "{key}: {e}"
            );
        }
        let (s, http) = make(json!({
            "app": "Notes", "scope": "outside", "decrypt": true, "kind": "http",
            "origin": "https://notes.example.org",
        }))
        .await;
        assert_eq!(s, 200, "{http}");
        let list = |path: &'static str| {
            let app = app.clone();
            async move { call(&app, "GET", path, "t0ken", Value::Null).await }
        };
        let (_, rows) = list("/accounts/odoo-partner-1/connections").await;
        let row = &rows[0];
        assert_eq!(row["id"], http["id"]);
        assert_eq!(
            (
                row["decrypt"].as_bool(),
                row["origin"].as_str(),
                row["kind"].as_str(),
                row["revoked_at"].clone()
            ),
            (
                Some(true),
                Some("https://notes.example.org"),
                Some("http"),
                Value::Null
            )
        );
        // the API's decrypt through a connection keeps to the consent; encrypt does not need it
        let peer = nostr::prelude::Keys::generate().public_key().to_hex();
        let crypt = |op: &'static str, connection: &Value, text: String| {
            let (app, connection, peer) = (app.clone(), connection["id"].clone(), peer.clone());
            async move {
                call(
                    &app,
                    "POST",
                    &format!("/accounts/odoo-partner-1/{op}"),
                    "t0ken",
                    json!({"peer": peer, "text": text, "connection": connection}),
                )
                .await
            }
        };
        let (s, sealed) = crypt("encrypt", &plain, "psst".into()).await;
        assert_eq!(s, 200, "{sealed}");
        let sealed = sealed["result"].as_str().unwrap().to_string();
        let (s, e) = crypt("decrypt", &plain, sealed.clone()).await;
        assert_eq!((s, e["error"].as_str()), (403, Some("forbidden")));
        assert_eq!(e["message"], "not allowed for this app");
        let (s, r) = crypt("decrypt", &http, sealed.clone()).await;
        assert_eq!((s, r["result"].as_str()), (200, Some("psst")));
        // the member's word, given later and taken back
        let consent = |vine_id: &'static str, body: Value| {
            let app = app.clone();
            async move {
                call(
                    &app,
                    "POST",
                    &format!("/accounts/{vine_id}/connections/consent"),
                    "t0ken",
                    body,
                )
                .await
            }
        };
        let (s, r) = consent(
            "odoo-partner-1",
            json!({"id": plain["id"], "decrypt": true}),
        )
        .await;
        assert_eq!((s, r["decrypt"].as_bool()), (200, Some(true)));
        assert_eq!(crypt("decrypt", &plain, sealed.clone()).await.0, 200);
        let by_key = json!({"bunker_pubkey": plain["bunker_pubkey"], "decrypt": false});
        assert_eq!(consent("odoo-partner-1", by_key.clone()).await.0, 200);
        assert_eq!(crypt("decrypt", &plain, sealed.clone()).await.0, 403);
        // not another account's connection, not without saying which, not a maybe
        assert_eq!(consent("guest-2", by_key).await.0, 404);
        for body in [
            json!({"decrypt": true}),
            json!({"id": plain["id"], "decrypt": "true"}),
            json!({"id": plain["id"]}),
        ] {
            let (s, e) = consent("odoo-partner-1", body).await;
            assert_eq!((s, e["error"].as_str()), (422, Some("invalid")));
        }
        let (s, _) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections/consent",
            "wrong",
            json!({}),
        )
        .await;
        assert_eq!(s, 401);

        // the list of all rows: revoked ones too, said as such
        call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections/revoke",
            "t0ken",
            json!({"id": plain["id"]}),
        )
        .await;
        let (_, live) = list("/accounts/odoo-partner-1/connections").await;
        assert_eq!(live.as_array().unwrap().len(), 1);
        let (_, same) = list("/accounts/odoo-partner-1/connections?all=false").await;
        assert_eq!(same, live);
        let (s, all) = list("/accounts/odoo-partner-1/connections?all=true").await;
        assert_eq!((s, all.as_array().unwrap().len()), (200, 2));
        let gone = all
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["id"] == plain["id"])
            .unwrap();
        assert!(gone["revoked_at"].as_i64().unwrap() > 0);
        for hidden in ["secret", "secret_hash", "bunker_sealed"] {
            assert!(gone.get(hidden).is_none(), "{hidden}");
        }
        assert_eq!(
            list("/accounts/odoo-partner-1/connections?all=maybe")
                .await
                .0,
            422
        );
        assert_eq!(
            list("/accounts/odoo-partner-9/connections?all=true")
                .await
                .0,
            404
        );
        // a sign-in over HTTP nobody connected to stays past its deadline
        signer
            .store
            .set_expires_at(http["id"].as_str().unwrap(), crate::store::now() - 1);
        let (_, live) = list("/accounts/odoo-partner-1/connections").await;
        assert_eq!(live.as_array().unwrap().len(), 1);
        let (s, _) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/sign",
            "t0ken",
            json!({"event": {"kind": 1}, "connection": http["id"]}),
        )
        .await;
        assert_eq!(s, 200);
        let (s, e) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/sign",
            "t0ken",
            json!({"event": {"kind": 30078, "tags": [["d", "belfry:read"]]}, "connection": http["id"]}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (403, Some("forbidden")));
    }

    #[tokio::test]
    async fn imported_sessions_carry_consent_and_origin_and_revoked_ones_can_be_read() {
        use crate::keys;
        use nostr::prelude::Keys;
        let (app, _) = app("carry-origin");
        call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        let account = keys::derive_keys(&[7u8; 32], "odoo-partner-1", 0);
        let hash = |n: u32| format!("$2b$10$N9qo8uLOickgx2ZMRZoMye{n:031}");
        let row = |n: u32, more: Value| {
            let mut r = json!({
                "bunker_pubkey": keys::keycast_bunker_keys(&account, &hash(n)).public_key().to_hex(),
                "keycast_secret_hash": hash(n),
                "client_pubkey": Keys::generate().public_key().to_hex(),
                "app": "Notes",
                "scope": "outside",
                "created_at": 1_700_000_000,
            });
            for (k, v) in more.as_object().unwrap() {
                r[k] = v.clone();
            }
            r
        };
        let import = |rows: Value| {
            let app = app.clone();
            async move {
                call(
                    &app,
                    "POST",
                    "/import/connections",
                    "t0ken",
                    json!({"vine_id": "odoo-partner-1", "connections": rows}),
                )
                .await
            }
        };
        for bad in [
            json!({"decrypt": "yes"}),
            json!({"origin": 5}),
            json!({"origin": "x".repeat(201)}),
        ] {
            let (s, e) = import(json!([row(1, bad)])).await;
            assert_eq!((s, e["error"].as_str()), (422, Some("invalid")), "{e}");
        }
        let (s, r) = import(json!([
            row(
                1,
                json!({"origin": "https://chat.belfry.test", "decrypt": true})
            ),
            row(2, json!({})),
            row(
                3,
                json!({"revoked_at": 1_700_001_000, "origin": "https://old.example.org"})
            ),
        ]))
        .await;
        assert_eq!(s, 200, "{r}");
        assert_eq!(r, json!({"imported": 2, "existing": 0, "revoked": 1}));
        let (_, live) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!(live.as_array().unwrap().len(), 2);
        let (_, all) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1/connections?all=true",
            "t0ken",
            Value::Null,
        )
        .await;
        let by_origin = |origin: &str| {
            all.as_array()
                .unwrap()
                .iter()
                .find(|c| c["origin"] == origin)
                .cloned()
                .unwrap()
        };
        let first = by_origin("https://chat.belfry.test");
        assert_eq!(
            (first["decrypt"].as_bool(), first["revoked_at"].clone()),
            (Some(true), Value::Null)
        );
        assert_eq!(by_origin("")["decrypt"], false);
        let old = by_origin("https://old.example.org");
        assert_eq!(old["revoked_at"], 1_700_001_000);
        assert_eq!(old["kind"], "nip46");
    }

    #[tokio::test]
    async fn the_session_import_says_clearly_when_a_call_is_too_large() {
        let (app, _) = app("too-large");
        call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        let import = |body: Value| {
            let app = app.clone();
            async move { call(&app, "POST", "/import/connections", "t0ken", body).await }
        };
        // more than the route's megabyte: 413 with a reason, not a closed connection
        let padding = "x".repeat(MAX_IMPORT_BODY);
        let (s, e) =
            import(json!({"vine_id": "odoo-partner-1", "connections": [], "pad": padding})).await;
        assert_eq!((s, e["error"].as_str()), (413, Some("too_large")), "{e}");
        assert!(e["message"].as_str().unwrap().contains("smaller batches"));
        // just under it, with room for what the migration sends (400 rows, 256 kB): read
        let padding = "x".repeat(MAX_IMPORT_BODY - 200);
        let (s, r) =
            import(json!({"vine_id": "odoo-partner-1", "connections": [], "pad": padding})).await;
        assert_eq!((s, r["imported"].as_i64()), (200, Some(0)), "{r}");
        const { assert!(MAX_IMPORT_BODY >= 4 * 256 * 1024 && MAX_IMPORTED_CONNECTIONS >= 2 * 400) };
        // too many rows: said as such
        let rows: Vec<Value> = (0..=MAX_IMPORTED_CONNECTIONS).map(|_| json!({})).collect();
        let (s, e) = import(json!({"vine_id": "odoo-partner-1", "connections": rows})).await;
        assert_eq!((s, e["error"].as_str()), (422, Some("invalid")));
        // a body that is not JSON: the API's own error shape too
        let req = Request::builder()
            .method("POST")
            .uri("/import/connections")
            .header("content-type", "application/json")
            .header("authorization", "Bearer t0ken")
            .body(Body::from("{not json"))
            .unwrap();
        let resp = app.clone().oneshot(req).await.unwrap();
        assert_eq!(resp.status().as_u16(), 422);
        // the other routes keep the default limit: a larger account import still goes through
        let (s, _) = call(
            &app,
            "POST",
            "/import",
            "t0ken",
            json!({"accounts": [], "pad": "x".repeat(MAX_IMPORT_BODY + 1000)}),
        )
        .await;
        assert_eq!(s, 200);
    }

    #[tokio::test]
    async fn no_new_accounts_while_the_switch_is_on() {
        let (app, signer) = app("closed");
        let (s, a) = call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        assert_eq!(s, 200);
        let (s, st) = call(&app, "GET", "/settings", "t0ken", Value::Null).await;
        assert_eq!(
            (s, st),
            (
                200,
                json!({"no_new_accounts": false, "by_environment": false})
            )
        );
        assert_eq!(
            call(&app, "GET", "/settings", "wrong", Value::Null).await.0,
            401
        );
        for body in [
            json!({}),
            json!({"no_new_accounts": "true"}),
            json!({"no_new_accounts": 1}),
        ] {
            let (s, e) = call(&app, "POST", "/settings", "t0ken", body).await;
            assert_eq!((s, e["error"].as_str()), (422, Some("invalid")));
        }
        let (s, st) = call(
            &app,
            "POST",
            "/settings",
            "t0ken",
            json!({"no_new_accounts": true}),
        )
        .await;
        assert_eq!((s, st["no_new_accounts"].as_bool()), (200, Some(true)));

        // an unknown account is not made: a distinct error, and nothing is written
        let (s, e) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-2",
            "t0ken",
            json!({"username": "lina"}),
        )
        .await;
        assert_eq!(
            (s, e["error"].as_str()),
            (409, Some("no_new_accounts")),
            "{e}"
        );
        assert_eq!(
            call(
                &app,
                "GET",
                "/accounts/odoo-partner-2",
                "t0ken",
                Value::Null
            )
            .await
            .0,
            404
        );
        assert_eq!(
            call(&app, "GET", "/names/lina", "t0ken", Value::Null)
                .await
                .0,
            404
        );
        // the ones that exist answer as always, and sign
        let (s, same) = call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        assert_eq!((s, &same["pubkey"]), (200, &a["pubkey"]));
        let (s, _) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/sign",
            "t0ken",
            json!({"event": {"kind": 1}}),
        )
        .await;
        assert_eq!(s, 200);
        // what the import does goes on: a member's own key, a sealed key, and an account whose
        // key the caller names (it is made only if it comes out as that key)
        let own = nostr::prelude::Keys::generate().public_key().to_hex();
        let (s, o) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-3",
            "t0ken",
            json!({"own_pubkey": own}),
        )
        .await;
        assert_eq!((s, o["custody"].as_str()), (200, Some("own")), "{o}");
        let derived = crate::keys::derive_keys(&[7u8; 32], "odoo-partner-4", 0);
        let (s, e) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-4",
            "t0ken",
            json!({"expect_pubkey": own}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (409, Some("mismatch")));
        let (s, d) = call(
            &app,
            "PUT",
            "/accounts/odoo-partner-4",
            "t0ken",
            json!({"expect_pubkey": derived.public_key().to_hex()}),
        )
        .await;
        assert_eq!((s, d["custody"].as_str()), (200, Some("derived")), "{d}");
        let random = nostr::prelude::Keys::generate();
        let (s, r) = call(
            &app,
            "POST",
            "/import",
            "t0ken",
            json!({"accounts": [{
                "vine_id": "guest-5",
                "pubkey": random.public_key().to_hex(),
                "sealed": hex::encode(signer.seal_legacy_for_tests(&random)),
            }]}),
        )
        .await;
        assert_eq!(
            (s, r["results"][0]["result"].as_str()),
            (200, Some("imported")),
            "{r}"
        );

        // the list of accounts, by status and by page
        call(
            &app,
            "POST",
            "/accounts/odoo-partner-3/status",
            "t0ken",
            json!({"status": "suspended"}),
        )
        .await;
        let ids = |v: &Value| {
            v["accounts"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| a["vine_id"].as_str().unwrap().to_string())
                .collect::<Vec<_>>()
        };
        let (s, all) = call(&app, "GET", "/accounts", "t0ken", Value::Null).await;
        assert_eq!(s, 200);
        assert_eq!(
            ids(&all),
            [
                "guest-5",
                "odoo-partner-1",
                "odoo-partner-3",
                "odoo-partner-4"
            ]
        );
        assert_eq!(
            (
                all["total"].as_i64(),
                all["suspended"].as_i64(),
                all["next"].clone()
            ),
            (Some(4), Some(1), Value::Null)
        );
        assert!(all["accounts"][0].get("sealed").is_none());
        assert_eq!(all["accounts"][0]["custody"], "imported");
        let (_, sus) = call(
            &app,
            "GET",
            "/accounts?status=suspended",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!(ids(&sus), ["odoo-partner-3"]);
        assert_eq!(sus["accounts"][0]["status"], "suspended");
        let (_, act) = call(
            &app,
            "GET",
            "/accounts?status=active&limit=2",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!(ids(&act), ["guest-5", "odoo-partner-1"]);
        assert_eq!(act["next"], "odoo-partner-1");
        let (_, rest) = call(
            &app,
            "GET",
            "/accounts?status=active&limit=2&after=odoo-partner-1",
            "t0ken",
            Value::Null,
        )
        .await;
        assert_eq!(ids(&rest), ["odoo-partner-4"]);
        assert_eq!(rest["next"], Value::Null);
        for bad in [
            "/accounts?status=banned",
            "/accounts?limit=0",
            "/accounts?limit=5000",
        ] {
            let (s, e) = call(&app, "GET", bad, "t0ken", Value::Null).await;
            assert_eq!((s, e["error"].as_str()), (422, Some("invalid")), "{bad}");
        }
        assert_eq!(
            call(&app, "GET", "/accounts", "wrong", Value::Null).await.0,
            401
        );

        // lifted: accounts are made again
        let (_, st) = call(
            &app,
            "POST",
            "/settings",
            "t0ken",
            json!({"no_new_accounts": false}),
        )
        .await;
        assert_eq!(st["no_new_accounts"], false);
        let (s, _) = call(&app, "PUT", "/accounts/odoo-partner-2", "t0ken", json!({})).await;
        assert_eq!(s, 200);
    }

    #[tokio::test]
    async fn the_environment_closes_accounts_for_good() {
        let mut signer = signer_with_seed(&tempdir("closed-env"));
        signer.no_new_accounts_env = true;
        let app = router(AppState {
            signer: Arc::new(signer),
            token: Arc::new("t0ken".into()),
        });
        let (_, st) = call(&app, "GET", "/settings", "t0ken", Value::Null).await;
        assert_eq!(st, json!({"no_new_accounts": true, "by_environment": true}));
        let (s, e) = call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        assert_eq!((s, e["error"].as_str()), (409, Some("no_new_accounts")));
        // the API cannot open what the environment closes
        let (s, e) = call(
            &app,
            "POST",
            "/settings",
            "t0ken",
            json!({"no_new_accounts": false}),
        )
        .await;
        assert_eq!((s, e["error"].as_str()), (422, Some("invalid")), "{e}");
        let (_, st) = call(&app, "GET", "/settings", "t0ken", Value::Null).await;
        assert_eq!(st["no_new_accounts"], true);
    }

    #[tokio::test]
    async fn a_connection_is_not_handed_out_while_the_bunker_is_not_listening() {
        let (app, signer) = app("bunker-down");
        call(&app, "PUT", "/accounts/odoo-partner-1", "t0ken", json!({})).await;
        // the bunker is on and its relay is away (or the subscription failed): nothing covers
        // a new connection
        signer.bunker.enabled.store(true, Ordering::SeqCst);
        let client = nostr::prelude::Keys::generate().public_key().to_hex();
        let (s, e) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            json!({"app": "Chat", "client_pubkey": client}),
        )
        .await;
        assert_eq!(
            (s, e["error"].as_str()),
            (503, Some("bunker_unavailable")),
            "{e}"
        );
        assert!(e.get("bunker_url").is_none());
        let (_, list) = call(
            &app,
            "GET",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            Value::Null,
        )
        .await;
        assert!(list.as_array().unwrap().is_empty(), "taken back: {list}");
        // a sign-in over HTTP does not need the bunker
        let (s, http) = call(
            &app,
            "POST",
            "/accounts/odoo-partner-1/connections",
            "t0ken",
            json!({"app": "Wallet", "kind": "http"}),
        )
        .await;
        assert_eq!((s, http["kind"].as_str()), (200, Some("http")), "{http}");
        // the bunker subscribes while the caller waits: handed out
        let waiter = {
            let (app, client) = (app.clone(), client.clone());
            tokio::spawn(async move {
                call(
                    &app,
                    "POST",
                    "/accounts/odoo-partner-1/connections",
                    "t0ken",
                    json!({"app": "Chat", "client_pubkey": client}),
                )
                .await
            })
        };
        for _ in 0..100 {
            tokio::time::sleep(Duration::from_millis(10)).await;
            let n = signer.bunker.changes();
            signer.bunker.subscribed.send_replace(n);
            if waiter.is_finished() {
                break;
            }
        }
        let (s, c) = waiter.await.unwrap();
        assert_eq!(s, 200, "{c}");
        assert!(c["bunker_url"].as_str().unwrap().starts_with("bunker://"));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn two_first_calls_at_once_never_replace_a_key() {
        let (app, signer) = app("race");
        // sixteen requests at once for one new account, each naming another key as the
        // member's own: one makes the account, the others hear that it is not theirs
        let mut calls = Vec::new();
        for _ in 0..16 {
            let app = app.clone();
            let own = nostr::prelude::Keys::generate().public_key().to_hex();
            calls.push(tokio::spawn(async move {
                let (s, r) = call(
                    &app,
                    "PUT",
                    "/accounts/odoo-partner-1",
                    "t0ken",
                    json!({"own_pubkey": own}),
                )
                .await;
                (s, r, own)
            }));
        }
        let mut winners = Vec::new();
        for c in calls {
            let (s, r, own) = c.await.unwrap();
            match s {
                200 => {
                    assert_eq!(r["pubkey"], own.as_str());
                    winners.push(own);
                }
                409 => assert_eq!(r["error"], "mismatch"),
                other => panic!("{other}: {r}"),
            }
        }
        assert_eq!(winners.len(), 1, "one key, the first one's");
        assert_eq!(signer.account("odoo-partner-1").unwrap().pubkey, winners[0]);
        // the same with derived keys and plain threads: everyone gets the one account
        let handles: Vec<_> = (0..8)
            .map(|i| {
                let signer = signer.clone();
                std::thread::spawn(move || {
                    signer
                        .ensure_account("guest-7", 0, None, None, Some(&format!("guest-{i}")))
                        .map(|a| (a.pubkey, a.username))
                })
            })
            .collect();
        let results: Vec<_> = handles
            .into_iter()
            .map(|h| h.join().unwrap().unwrap())
            .collect();
        assert!(results.iter().all(|r| r == &results[0]), "{results:?}");
        let stored = signer.account("guest-7").unwrap();
        assert_eq!((stored.pubkey, stored.username), results[0].clone());
    }
}
