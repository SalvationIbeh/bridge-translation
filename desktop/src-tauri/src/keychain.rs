//! OS keychain storage for auth secrets (Phase E / Option C — see
//! docs/decisions/2026-06-20-ws-auth-resumption-token.md).
//!
//! Two secrets live here, never in a plaintext field or settings file:
//! - `bootstrap` — the durable per-partner credential, presented on first connect (and as the
//!   fallback when a resumption token is rejected). Saved when the operator pastes a fresh token.
//! - `resumption` — the single-use HMAC token the server rotates into every `ready` frame; cached
//!   so a quick restart can resume within its TTL. A stale one is harmless (the reconnect loop
//!   falls back to `bootstrap` on AUTH_FAILED).
//!
//! On Windows this is the Credential Manager (keyring `windows-native`). Failures are non-fatal:
//! the app degrades to the pasted token for the session rather than refusing to run.

use keyring::Entry;

const SERVICE: &str = "com.vocivio.desktop";

pub const BOOTSTRAP: &str = "bootstrap";
pub const RESUMPTION: &str = "resumption";

fn entry(key: &str) -> Option<Entry> {
    match Entry::new(SERVICE, key) {
        Ok(e) => Some(e),
        Err(e) => {
            tracing::warn!("keychain entry {key:?} unavailable: {e}");
            None
        }
    }
}

/// Persist a secret under `key`. Best-effort: a failure is logged, not propagated.
pub fn store(key: &str, secret: &str) {
    if let Some(e) = entry(key) {
        if let Err(err) = e.set_password(secret) {
            tracing::warn!("keychain store {key:?} failed: {err}");
        }
    }
}

/// Load a secret, or `None` if absent/unreadable.
pub fn load(key: &str) -> Option<String> {
    let e = entry(key)?;
    match e.get_password() {
        Ok(s) => Some(s),
        Err(keyring::Error::NoEntry) => None,
        Err(err) => {
            tracing::warn!("keychain load {key:?} failed: {err}");
            None
        }
    }
}

/// Clear a secret (best-effort) — used when an operator disconnects/forgets a credential.
pub fn clear(key: &str) {
    if let Some(e) = entry(key) {
        // NoEntry is fine; anything else is logged.
        match e.delete_credential() {
            Ok(_) | Err(keyring::Error::NoEntry) => {}
            Err(err) => tracing::warn!("keychain clear {key:?} failed: {err}"),
        }
    }
}
