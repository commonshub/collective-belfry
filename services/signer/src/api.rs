//! The internal HTTP API. Only the onboarding service calls it (Compose network `signer`, no
//! published port), with the bearer token from `/run/secrets/signer_token`.
//!
//! ```text
//! GET  /health                                  {status, seed, accounts}   (no token)
//! PUT  /accounts/{vine_id}                      {key_version?, own_pubkey?, expect_pubkey?}
//! GET  /accounts/{vine_id}
//! POST /accounts/{vine_id}/status               {status: active|suspended}
//! POST /accounts/{vine_id}/sign                 {event}                    -> {event}
//! POST /accounts/{vine_id}/encrypt              {scheme: nip44|nip04, peer, text} -> {result}
//! POST /accounts/{vine_id}/decrypt              {scheme, peer, text}       -> {result}
//! POST /accounts/{vine_id}/derive               {purpose}                  -> {secret}
//! POST /accounts/{vine_id}/export                                          -> {nsec}
//! POST /accounts/{vine_id}/connections          {app, label, derive?, keep?} -> {id, bunker_url, ...}
//! GET  /accounts/{vine_id}/connections                                     -> [{id, app, ...}]
//! POST /accounts/{vine_id}/connections/revoke   {id?, app?}                -> {revoked}
//! POST /import                                  {accounts: [{vine_id, pubkey, sealed}]}
//! ```
//!
//! sign, encrypt, decrypt and derive take an optional `connection` (the id behind an app's
//! access token): it must be live, and derive allows only that connection's purposes.
//!
//! Errors are `{"error": <code>, "message"}` with 404 not_found, 503 seed_locked,
//! 409 own_key / mismatch, 403 suspended / forbidden, 422 invalid, 401 unauthorized.

use std::sync::Arc;

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
            Error::OwnKey | Error::Mismatch(_) => StatusCode::CONFLICT,
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
    })
}

async fn health(State(state): State<AppState>) -> Json<Value> {
    let accounts = state.signer.store.count_accounts().unwrap_or(-1);
    Json(json!({"status": "ok", "seed": state.signer.seed_state(), "accounts": accounts}))
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
    let created =
        state
            .signer
            .create_connection(&vine_id, &body.app, &label, &body.derive, body.keep)?;
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
    let app = opt_text(&body, "app");
    let n = state
        .signer
        .revoke(&vine_id, id.as_deref(), app.as_deref())?;
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
}
