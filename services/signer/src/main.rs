//! belfry-signer: holds the members' Nostr keys and signs as them.
//!
//! - an internal HTTP API for the onboarding service (`api.rs`), on the Compose network only;
//! - a NIP-46 bunker on the bunker relay (`bunker.rs`), one key per app connection.
//!
//! Configuration (environment):
//!
//! | Variable | Meaning |
//! | --- | --- |
//! | `SIGNER_LISTEN` | address of the API, default `0.0.0.0:8200` |
//! | `SIGNER_TOKEN_FILE` | the API's bearer token, default `/run/secrets/signer_token` |
//! | `SIGNER_MASTER_KEY_FILE` | 32 bytes base64 that seal the keys at rest (Keycast's master.key format), default `/run/secrets/signer_master_key` |
//! | `SIGNER_DB` | the SQLite file, default `/data/signer.sqlite3` |
//! | `CUSTODY_SEED_FILE` | the custody seed (64 hex), read on every account creation |
//! | `BUNKER_RELAYS` | relays the bunker listens on, comma-separated (`ws://bunker-relay:8080`) |
//! | `BUNKER_PUBLIC_RELAYS` | the same relays as clients reach them, for bunker:// URLs |
//! | `SIGNER_UNCLAIMED_SECONDS` | how long a connection made without a client key waits for its first `connect`, default 600 |
//! | `SIGNER_OWN_DOMAIN` | the belfry's domain: what an outside app's connection never signs for (`limits.rs`) |
//! | `SIGNER_OWN_API_HOSTS`, `SIGNER_OWN_RELAY_HOSTS`, `SIGNER_OWN_BLOSSOM_HOSTS` | more of the belfry's own hosts or URLs, separated by commas or spaces |
//!
//! `belfry-signer derive <vine_id> <version>` prints the public key the seed gives (a check
//! without the service running).

mod api;
mod bunker;
mod keys;
mod limits;
mod signer;
mod store;
mod vault;

use std::path::PathBuf;
use std::sync::Arc;

use tracing_subscriber::EnvFilter;

fn env(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn list(name: &str) -> Vec<String> {
    env(name, "")
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

fn fail(msg: String) -> ! {
    eprintln!("belfry-signer: {msg}");
    std::process::exit(2)
}

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();
    let args: Vec<String> = std::env::args().collect();
    let seed_path = std::env::var("CUSTODY_SEED_FILE")
        .ok()
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);

    if args.get(1).map(String::as_str) == Some("derive") {
        let (Some(vine_id), version) = (
            args.get(2),
            args.get(3).and_then(|v| v.parse().ok()).unwrap_or(0),
        ) else {
            fail("usage: belfry-signer derive <vine_id> [<version>]".into())
        };
        let seed =
            keys::read_seed(seed_path.as_deref()).unwrap_or_else(|e| fail(format!("seed: {e:?}")));
        println!(
            "{}",
            keys::derive_keys(&seed, vine_id, version)
                .public_key()
                .to_hex()
        );
        return;
    }
    if args.get(1).map(String::as_str) == Some("healthcheck") {
        // for the container's healthcheck (no curl in the image)
        let addr = env("SIGNER_LISTEN", "0.0.0.0:8200").replace("0.0.0.0", "127.0.0.1");
        let ok = std::net::TcpStream::connect(addr).is_ok();
        std::process::exit(if ok { 0 } else { 1 });
    }

    let token = std::fs::read_to_string(env("SIGNER_TOKEN_FILE", "/run/secrets/signer_token"))
        .map(|t| t.trim().to_string())
        .unwrap_or_else(|e| fail(format!("SIGNER_TOKEN_FILE: {e}")));
    if token.len() < 16 {
        fail("the API token must be at least 16 characters".into());
    }
    let vault = vault::Vault::from_file(&PathBuf::from(env(
        "SIGNER_MASTER_KEY_FILE",
        "/run/secrets/signer_master_key",
    )))
    .unwrap_or_else(|e| fail(e.0));
    let store = store::Store::open(&PathBuf::from(env("SIGNER_DB", "/data/signer.sqlite3")))
        .unwrap_or_else(|e| fail(format!("SIGNER_DB: {e}")));
    let mut signer = signer::Signer::new(store, vault, seed_path, list("BUNKER_PUBLIC_RELAYS"));
    signer.unclaimed_ttl = env("SIGNER_UNCLAIMED_SECONDS", "600")
        .parse()
        .ok()
        .filter(|s| *s > 0)
        .unwrap_or_else(|| fail("SIGNER_UNCLAIMED_SECONDS must be a number of seconds".into()));
    signer.own = limits::Own::from_config(
        &env("SIGNER_OWN_DOMAIN", ""),
        &env("SIGNER_OWN_API_HOSTS", ""),
        &env("SIGNER_OWN_RELAY_HOSTS", ""),
        &env("SIGNER_OWN_BLOSSOM_HOSTS", ""),
    );
    if signer.own.domain.is_empty() {
        tracing::warn!(
            "no SIGNER_OWN_DOMAIN: connections of outside apps are not kept from the belfry's \
             own hosts under its domain (only from the hosts listed in SIGNER_OWN_*_HOSTS)"
        );
    }
    // keys sealed the old way (no row binding) are bound to their rows once; one that does
    // not open, or holds another key than its row names, stops the start
    match signer.upgrade_seals() {
        Ok(0) => {}
        Ok(n) => tracing::info!(n, "sealed keys bound to their rows"),
        Err(e) => fail(format!("sealed keys: {}", e.message())),
    }
    let signer = Arc::new(signer);
    let listen = env("SIGNER_LISTEN", "0.0.0.0:8200");

    let runtime = tokio::runtime::Runtime::new().unwrap_or_else(|e| fail(format!("runtime: {e}")));
    runtime.block_on(async move {
        tokio::spawn(bunker::run(signer.clone(), list("BUNKER_RELAYS")));
        let app = api::router(api::AppState {
            signer: signer.clone(),
            token: Arc::new(token),
        });
        let listener = tokio::net::TcpListener::bind(&listen)
            .await
            .unwrap_or_else(|e| fail(format!("listen {listen}: {e}")));
        tracing::info!(%listen, seed = signer.seed_state(), "belfry-signer up");
        axum::serve(listener, app)
            .with_graceful_shutdown(async {
                let _ = tokio::signal::ctrl_c().await;
            })
            .await
            .unwrap_or_else(|e| fail(format!("serve: {e}")));
    });
}
