//! Key derivation: the members' keys from the custody seed, and per-purpose secrets from a key.
//!
//! Both formulas are the ones Keycast used with Belfry's patches 0014 and 0011, so every npub
//! and every wallet secret stays the same after the move:
//!
//! - member key: `HKDF-SHA256(salt "belfry/custody/v1", IKM = seed,
//!   info "belfry/nostr/v1/<vine_id>/<version>")`; an output outside the curve order (about
//!   2^-128) moves on to `info "…/<version>#<counter>"`.
//! - per-purpose secret: `HKDF-SHA256(salt "keycast/derive/v1", IKM = the 32-byte secret key,
//!   info = purpose)`. The salt keeps Keycast's name on purpose: changing it would change the
//!   wallets' seeds.
//!
//! A third formula is Keycast's own and is used only by the session import
//! (`POST /import/connections`): Keycast never stored a session's bunker key, it derived it,
//! `HKDF-SHA256(no salt, IKM = the account's 32-byte secret key,
//! info "keycast-bunker-nip46-v1-<secret_hash>")` with the row's stored `secret_hash` text
//! (upstream `core/src/bunker_key.rs` at fe9c646); an output outside the curve order moves on
//! to `info "…<secret_hash>-retry<counter>"`, counter 1, 2, ….
//!
//! The seed file holds 64 hex characters. It is read on every use, so a steward can unlock it
//! (`infra/scripts/custody-seed.sh unlock`) without restarting the signer; while it is missing,
//! creating an account answers 503 instead of making a key that could not be recovered.

use std::path::Path;

use hkdf::Hkdf;
use nostr::prelude::{Keys, SecretKey};
use sha2::Sha256;
use zeroize::Zeroizing;

const CUSTODY_SALT: &[u8] = b"belfry/custody/v1";
const DERIVE_SALT: &[u8] = b"keycast/derive/v1";

#[derive(Debug, PartialEq, Eq)]
pub enum SeedError {
    /// No seed file configured, or the file is missing (not unlocked yet).
    Locked,
    /// The file is not 64 hex characters.
    Malformed,
}

pub fn read_seed(path: Option<&Path>) -> Result<Zeroizing<[u8; 32]>, SeedError> {
    let path = path.ok_or(SeedError::Locked)?;
    let text = Zeroizing::new(std::fs::read_to_string(path).map_err(|_| SeedError::Locked)?);
    parse_seed(&text)
}

fn parse_seed(text: &str) -> Result<Zeroizing<[u8; 32]>, SeedError> {
    let bytes = Zeroizing::new(hex::decode(text.trim()).map_err(|_| SeedError::Malformed)?);
    let mut out = Zeroizing::new([0u8; 32]);
    if bytes.len() != 32 {
        return Err(SeedError::Malformed);
    }
    out.copy_from_slice(&bytes);
    Ok(out)
}

/// The member's key for `vine_id` (`odoo-partner-<id>`) and `version`.
pub fn derive_keys(seed: &[u8; 32], vine_id: &str, version: u32) -> Keys {
    let hk = Hkdf::<Sha256>::new(Some(CUSTODY_SALT), seed);
    for counter in 0u32.. {
        let info = if counter == 0 {
            format!("belfry/nostr/v1/{vine_id}/{version}")
        } else {
            format!("belfry/nostr/v1/{vine_id}/{version}#{counter}")
        };
        let mut out = Zeroizing::new([0u8; 32]);
        hk.expand(info.as_bytes(), out.as_mut())
            .expect("32 bytes is a valid HKDF-SHA256 output length");
        if let Ok(secret) = SecretKey::from_slice(out.as_ref()) {
            return Keys::new(secret);
        }
    }
    unreachable!("the counter space is never exhausted")
}

/// A per-purpose secret derived from a member's key (the village wallet's seed and nutzap key).
pub fn derive_secret(keys: &Keys, purpose: &str) -> Zeroizing<[u8; 32]> {
    let secret = Zeroizing::new(keys.secret_key().to_secret_bytes());
    let hk = Hkdf::<Sha256>::new(Some(DERIVE_SALT), secret.as_ref());
    let mut out = Zeroizing::new([0u8; 32]);
    hk.expand(purpose.as_bytes(), out.as_mut())
        .expect("32 bytes is a valid HKDF-SHA256 output length");
    out
}

/// Keycast's domain separator for a session's bunker key.
const KEYCAST_BUNKER_INFO: &str = "keycast-bunker-nip46-v1-";

/// The HKDF info of try `counter` (0: the first) for a Keycast session's bunker key.
fn keycast_bunker_info(secret_hash: &str, counter: u32) -> Zeroizing<String> {
    Zeroizing::new(if counter == 0 {
        format!("{KEYCAST_BUNKER_INFO}{secret_hash}")
    } else {
        format!("{KEYCAST_BUNKER_INFO}{secret_hash}-retry{counter}")
    })
}

/// The first HKDF output `accept` takes, trying the infos of `keycast_bunker_info` in order.
fn keycast_bunker_search<T>(
    ikm: &[u8],
    secret_hash: &str,
    mut accept: impl FnMut(&[u8; 32]) -> Option<T>,
) -> T {
    let hk = Hkdf::<Sha256>::new(None, ikm);
    for counter in 0u32.. {
        let mut out = Zeroizing::new([0u8; 32]);
        hk.expand(
            keycast_bunker_info(secret_hash, counter).as_bytes(),
            out.as_mut(),
        )
        .expect("32 bytes is a valid HKDF-SHA256 output length");
        if let Some(found) = accept(&out) {
            return found;
        }
    }
    unreachable!("the counter space is never exhausted")
}

/// The bunker key Keycast derived for a session of the account with key `account`, from the
/// session's stored `oauth_authorizations.secret_hash` text.
pub fn keycast_bunker_keys(account: &Keys, secret_hash: &str) -> Keys {
    let ikm = Zeroizing::new(account.secret_key().to_secret_bytes());
    keycast_bunker_search(ikm.as_ref(), secret_hash, |out| {
        SecretKey::from_slice(out).ok().map(Keys::new)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const BCRYPT: &str = "$2b$10$N9qo8uLOickgx2ZMRZoMyeIjZAgcfl7p92ldGxad68LJZdL17lhWy";

    #[test]
    fn keycast_session_keys_follow_keycasts_formula() {
        // Computed once outside this code (Python hmac/hashlib, RFC 5869 by hand) from the
        // documented formula: no salt, IKM the secret key, info prefix + secret_hash.
        let one = Keys::new(SecretKey::from_slice(&[1u8; 32]).unwrap());
        assert_eq!(
            keycast_bunker_keys(&one, BCRYPT)
                .secret_key()
                .to_secret_hex(),
            "4d38285c03715fc76a40900b272df7f62fa8f9115c92b8e5a4a6254511b60818"
        );
        let ikm: Vec<u8> = (1u8..=32).collect();
        let counting = Keys::new(SecretKey::from_slice(&ikm).unwrap());
        let other = "$2b$12$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234";
        assert_eq!(
            keycast_bunker_keys(&counting, other)
                .secret_key()
                .to_secret_hex(),
            "30f12335aed487c0a40f5e90d62701ca16c19cc10ad571d132d9e45ff74a6222"
        );
        // an empty secret_hash is still a formula (Keycast's own test 6)
        assert_eq!(
            keycast_bunker_keys(&one, "").secret_key().to_secret_hex(),
            "eaf775be1e5bf4468fde2f51c912424b7235bf2aad8a57a8550c1e72eecfe2f3"
        );
        // and the same through a plain HKDF call here
        let mut plain = [0u8; 32];
        Hkdf::<Sha256>::new(None, &[1u8; 32])
            .expand(
                format!("keycast-bunker-nip46-v1-{BCRYPT}").as_bytes(),
                &mut plain,
            )
            .unwrap();
        assert_eq!(
            keycast_bunker_keys(&one, BCRYPT)
                .secret_key()
                .to_secret_hex(),
            hex::encode(plain)
        );
        assert_ne!(
            keycast_bunker_keys(&one, BCRYPT).public_key(),
            one.public_key()
        );
    }

    #[test]
    fn a_keycast_session_key_outside_the_curve_order_is_tried_again() {
        assert_eq!(
            keycast_bunker_info("h", 0).as_str(),
            "keycast-bunker-nip46-v1-h"
        );
        assert_eq!(
            keycast_bunker_info("h", 1).as_str(),
            "keycast-bunker-nip46-v1-h-retry1"
        );
        assert_eq!(
            keycast_bunker_info("h", 12).as_str(),
            "keycast-bunker-nip46-v1-h-retry12"
        );
        // the loop: an output that is refused moves on to the next info
        let mut tries = 0;
        let second = keycast_bunker_search(&[1u8; 32], BCRYPT, |out| {
            tries += 1;
            (tries == 2).then(|| hex::encode(out))
        });
        assert_eq!(
            second, "a4136c9da2946ec19703db48589e6b4f37c65b07f9e7f468549f794df67a7b8d",
            "info …-retry1, computed outside this code"
        );
        let mut tries = 0;
        let fourth = keycast_bunker_search(&[1u8; 32], BCRYPT, |out| {
            tries += 1;
            (tries == 4).then_some(*out)
        });
        let mut plain = [0u8; 32];
        Hkdf::<Sha256>::new(None, &[1u8; 32])
            .expand(
                format!("keycast-bunker-nip46-v1-{BCRYPT}-retry3").as_bytes(),
                &mut plain,
            )
            .unwrap();
        assert_eq!(fourth, plain);
    }

    #[test]
    fn member_keys_are_the_ones_keycast_derived() {
        // The vector of Keycast patch 0014 (and `custody-seed.sh derive`): seed 32 x 0x07.
        let keys = derive_keys(&[7u8; 32], "odoo-partner-42", 0);
        assert_eq!(
            keys.secret_key().to_secret_hex(),
            "67fe87d7cdac7cb85d61f170f6d898a157f6450f5b0c750a29442899fc186392"
        );
        assert_ne!(
            derive_keys(&[7u8; 32], "odoo-partner-43", 0).public_key(),
            keys.public_key()
        );
        assert_ne!(
            derive_keys(&[7u8; 32], "odoo-partner-42", 1).public_key(),
            keys.public_key()
        );
        assert_ne!(
            derive_keys(&[8u8; 32], "odoo-partner-42", 0).public_key(),
            keys.public_key()
        );
    }

    #[test]
    fn derived_secrets_are_the_ones_keycast_derived() {
        // The vector of Keycast patch 0011: IKM 32 x 0x01, purpose cashu-wallet-v1.
        let keys = Keys::new(SecretKey::from_slice(&[1u8; 32]).unwrap());
        assert_eq!(
            hex::encode(*derive_secret(&keys, "cashu-wallet-v1")),
            "71b029b419dbc314159f9548e501c25b87b30d740ffdc2b06c005afe266593cc"
        );
        assert_eq!(
            hex::encode(*derive_secret(&keys, "cashu-nutzap-v1")),
            "27594c30a00c51b02dfcd2a88ba6d486598d0ae4cf50a7e4d9475acd79f74ac3"
        );
    }

    #[test]
    fn seed_files_are_64_hex_characters() {
        assert_eq!(
            *parse_seed(&format!("{}\n", "ab".repeat(32))).unwrap(),
            [0xab; 32]
        );
        assert_eq!(parse_seed("abc").unwrap_err(), SeedError::Malformed);
        assert_eq!(
            parse_seed(&"zz".repeat(32)).unwrap_err(),
            SeedError::Malformed
        );
        assert_eq!(read_seed(None).unwrap_err(), SeedError::Locked);
        assert_eq!(
            read_seed(Some(Path::new("/nonexistent/seed"))).unwrap_err(),
            SeedError::Locked
        );
    }
}
