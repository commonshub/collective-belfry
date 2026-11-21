//! Secret keys at rest: AES-256-GCM under the master key, stored as `nonce (12) || ciphertext`.
//!
//! This is exactly Keycast's `FileKeyManager` format, and the master key file is the same
//! (32 random bytes, base64): the migration copies Keycast's encrypted keys as they are,
//! including keys made before the custody seed existed, which cannot be derived again.

use std::path::Path;

use aes_gcm::aead::{Aead, KeyInit, OsRng};
use aes_gcm::{AeadCore, Aes256Gcm, Nonce};
use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use nostr::prelude::{Keys, SecretKey};
use zeroize::Zeroizing;

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

    pub fn seal(&self, keys: &Keys) -> Vec<u8> {
        let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
        let secret = Zeroizing::new(keys.secret_key().to_secret_bytes());
        let mut out = nonce.to_vec();
        out.extend(
            self.cipher
                .encrypt(&nonce, secret.as_ref())
                .expect("AES-GCM encryption of 32 bytes does not fail"),
        );
        out
    }

    pub fn open(&self, sealed: &[u8]) -> Result<Keys, VaultError> {
        if sealed.len() < 12 {
            return Err(VaultError("sealed key too short".into()));
        }
        let (nonce, ciphertext) = sealed.split_at(12);
        let plain = Zeroizing::new(
            self.cipher
                .decrypt(Nonce::from_slice(nonce), ciphertext)
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
        let sealed = vault.seal(&keys);
        assert_eq!(sealed.len(), 12 + 32 + 16);
        assert_ne!(sealed, vault.seal(&keys), "a fresh nonce every time");
        assert_eq!(vault.open(&sealed).unwrap().public_key(), keys.public_key());
        let other = Vault::from_base64(&BASE64.encode([4u8; 32])).unwrap();
        assert!(other.open(&sealed).is_err());
        assert!(vault.open(&sealed[..10]).is_err());
        assert!(Vault::from_base64("c2hvcnQ=").is_err());
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
        assert_eq!(
            vault.open(&sealed).unwrap().secret_key().to_secret_bytes(),
            secret
        );
    }
}
