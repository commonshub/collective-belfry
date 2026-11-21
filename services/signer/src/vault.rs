//! Secret keys at rest: AES-256-GCM under the master key.
//!
//! Two formats:
//!
//! - **bound** (what the signer writes): `0x02 || nonce (12) || ciphertext`, with the row the
//!   key belongs to as associated data (`account:<vine_id>`, `connection:<id>`). A blob copied
//!   onto another row does not open.
//! - **legacy**: `nonce (12) || ciphertext`, no associated data. This is exactly Keycast's
//!   `FileKeyManager` format, and the master key file is the same (32 random bytes, base64):
//!   the migration copies Keycast's encrypted keys as they are, including keys made before the
//!   custody seed existed, which cannot be derived again. Legacy blobs are opened on import
//!   (`open_legacy`) and by the one-time upgrade at start (`Signer::upgrade_seals`), never on
//!   the signing path.
//!
//! Whatever opens, the caller compares the key with the public key stored on the row
//! (`open_for`): a blob that opens but holds another key is refused.

use std::path::Path;

use aes_gcm::aead::{Aead, KeyInit, OsRng, Payload};
use aes_gcm::{AeadCore, Aes256Gcm, Nonce};
use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use nostr::prelude::{Keys, SecretKey};
use zeroize::Zeroizing;

/// nonce (12) + secret key (32) + tag (16)
const LEGACY_LEN: usize = 12 + 32 + 16;
/// First byte of a bound blob.
const BOUND: u8 = 0x02;

pub struct Vault {
    cipher: Aes256Gcm,
}

#[derive(Debug)]
pub struct VaultError(pub String);

impl std::fmt::Display for VaultError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl Vault {
    pub fn from_file(path: &Path) -> Result<Self, VaultError> {
        let text = Zeroizing::new(
            std::fs::read_to_string(path)
                .map_err(|e| VaultError(format!("master key {}: {e}", path.display())))?,
        );
        Self::from_base64(&text)
    }

    pub fn from_base64(text: &str) -> Result<Self, VaultError> {
        let bytes = Zeroizing::new(
            BASE64
                .decode(text.trim())
                .map_err(|_| VaultError("master key is not base64".into()))?,
        );
        if bytes.len() != 32 {
            return Err(VaultError("master key must be 32 bytes".into()));
        }
        let cipher = Aes256Gcm::new_from_slice(&bytes)
            .map_err(|_| VaultError("master key must be 32 bytes".into()))?;
        Ok(Self { cipher })
    }

    /// Seals `keys` for the row `aad` (bound format).
    pub fn seal(&self, keys: &Keys, aad: &str) -> Vec<u8> {
        let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
        let secret = Zeroizing::new(keys.secret_key().to_secret_bytes());
        let mut out = vec![BOUND];
        out.extend_from_slice(&nonce);
        out.extend(
            self.cipher
                .encrypt(
                    &nonce,
                    Payload {
                        msg: secret.as_ref(),
                        aad: aad.as_bytes(),
                    },
                )
                .expect("AES-GCM encryption of 32 bytes does not fail"),
        );
        out
    }

    pub fn is_legacy(sealed: &[u8]) -> bool {
        sealed.len() == LEGACY_LEN
    }

    /// Opens a bound blob of the row `aad`. Legacy blobs are refused here.
    pub fn open(&self, sealed: &[u8], aad: &str) -> Result<Keys, VaultError> {
        if sealed.len() != LEGACY_LEN + 1 || sealed[0] != BOUND {
            return Err(VaultError(
                "sealed key is not in the bound format (run the upgrade)".into(),
            ));
        }
        self.decrypt(&sealed[1..], aad.as_bytes())
    }

    /// Opens a bound blob and checks that it holds the key `pubkey` (hex).
    pub fn open_for(&self, sealed: &[u8], aad: &str, pubkey: &str) -> Result<Keys, VaultError> {
        let keys = self.open(sealed, aad)?;
        if keys.public_key().to_hex() != pubkey {
            return Err(VaultError("sealed key is not the key of this row".into()));
        }
        Ok(keys)
    }

    /// Opens what Keycast sealed (`nonce || ciphertext`, no associated data).
    pub fn open_legacy(&self, sealed: &[u8]) -> Result<Keys, VaultError> {
        if sealed.len() != LEGACY_LEN {
            return Err(VaultError("sealed key has the wrong length".into()));
        }
        self.decrypt(sealed, b"")
    }

    /// Keycast's format, for the tests of the import and of the upgrade.
    #[cfg(test)]
    pub fn seal_legacy(&self, keys: &Keys) -> Vec<u8> {
        let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
        let secret = Zeroizing::new(keys.secret_key().to_secret_bytes());
        let mut out = nonce.to_vec();
        out.extend(self.cipher.encrypt(&nonce, secret.as_ref()).unwrap());
        out
    }

    fn decrypt(&self, sealed: &[u8], aad: &[u8]) -> Result<Keys, VaultError> {
        let (nonce, ciphertext) = sealed.split_at(12);
        let plain = Zeroizing::new(
            self.cipher
                .decrypt(
                    Nonce::from_slice(nonce),
                    Payload {
                        msg: ciphertext,
                        aad,
                    },
                )
                .map_err(|_| VaultError("sealed key does not open with this master key".into()))?,
        );
        let secret = SecretKey::from_slice(&plain)
            .map_err(|_| VaultError("sealed key is invalid".into()))?;
        Ok(Keys::new(secret))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sealed_keys_open_again_and_only_with_their_master_key() {
        let vault = Vault::from_base64(&BASE64.encode([3u8; 32])).unwrap();
        let keys = Keys::generate();
        let sealed = vault.seal(&keys, "account:a");
        assert_eq!(sealed.len(), 1 + 12 + 32 + 16);
        assert_ne!(
            sealed,
            vault.seal(&keys, "account:a"),
            "a fresh nonce every time"
        );
        assert_eq!(
            vault.open(&sealed, "account:a").unwrap().public_key(),
            keys.public_key()
        );
        let other = Vault::from_base64(&BASE64.encode([4u8; 32])).unwrap();
        assert!(other.open(&sealed, "account:a").is_err());
        assert!(vault.open(&sealed[..10], "account:a").is_err());
        assert!(Vault::from_base64("c2hvcnQ=").is_err());
    }

    #[test]
    fn a_sealed_key_is_bound_to_its_row_and_to_its_public_key() {
        let vault = Vault::from_base64(&BASE64.encode([3u8; 32])).unwrap();
        let (a, b) = (Keys::generate(), Keys::generate());
        let sealed = vault.seal(&a, "account:a");
        // copied onto another row: does not open
        assert!(vault.open(&sealed, "account:b").is_err());
        assert!(vault.open(&sealed, "connection:a").is_err());
        // opens, but the row says another key
        let pk = |k: &Keys| k.public_key().to_hex();
        assert!(vault.open_for(&sealed, "account:a", &pk(&a)).is_ok());
        assert!(vault.open_for(&sealed, "account:a", &pk(&b)).is_err());
        // a bound blob is not a legacy one, and the other way round
        assert!(vault.open_legacy(&sealed).is_err());
        assert!(vault.open_legacy(&sealed[1..]).is_err());
    }

    #[test]
    fn opens_what_keycast_sealed() {
        // Keycast's FileKeyManager: nonce || AES-256-GCM(master key, secret key bytes).
        let master = [9u8; 32];
        let vault = Vault::from_base64(&BASE64.encode(master)).unwrap();
        let cipher = Aes256Gcm::new_from_slice(&master).unwrap();
        let nonce = [5u8; 12];
        let secret = [0x11u8; 32];
        let mut sealed = nonce.to_vec();
        sealed.extend(
            cipher
                .encrypt(Nonce::from_slice(&nonce), &secret[..])
                .unwrap(),
        );
        assert!(Vault::is_legacy(&sealed));
        assert_eq!(
            vault
                .open_legacy(&sealed)
                .unwrap()
                .secret_key()
                .to_secret_bytes(),
            secret
        );
        // never on the signing path
        assert!(vault.open(&sealed, "account:a").is_err());
    }
}
