//! WebSocket client for `/ws/translate`. UI-agnostic: it exchanges typed channel messages
//! so the probe and the Tauri UI share it.
//!
//! **Reconnect + fail-open (B2):** [`run_with_reconnect`] is a dumb supervised loop —
//! `connect → pump → on-drop back off → reconnect`. There is NO app-level heartbeat
//! (tokio-tungstenite already does transport ping/pong). The auth token is re-read from a
//! shared store on every attempt, so a server-rotated resumption token (Option C) recovers the
//! session for free. While the socket is down/degraded the loop raises the `degraded` flag; the
//! mic pump reads it and copies input → output untranslated (fail-open passthrough), and the app
//! is told via [`Incoming::Conn`] ([`ConnPhase`]) so the UI can show an unmissable UNTRANSLATED state.
//! docs/decisions/2026-06-20-ws-auth-resumption-token.md

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::protocol::{ClientMsg, ServerMsg};
use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message};

/// Reconnect backoff schedule: start fast, double, cap at 8s (plan B2).
pub const MIN_BACKOFF: Duration = Duration::from_millis(500);
pub const MAX_BACKOFF: Duration = Duration::from_secs(8);
/// Floor for retrying after the server says it's not ready / at capacity — no point hammering a
/// warming or full node every 0.5s.
pub const BACKPRESSURE_BACKOFF: Duration = Duration::from_secs(3);

/// Next backoff value (doubles, capped). Pure so it's unit-testable.
pub(crate) fn next_backoff(cur: Duration) -> Duration {
    (cur * 2).min(MAX_BACKOFF)
}

/// Adopt a rotated resumption token from a `ready` frame. `None` (legacy static mode, or a frame
/// without the field) leaves the current token untouched so the next reconnect reuses it.
pub(crate) fn apply_resumption_token(store: &Arc<Mutex<String>>, token: Option<String>) {
    if let Some(t) = token {
        if let Ok(mut cur) = store.lock() {
            *cur = t;
        }
    }
}

/// On auth failure, fall back to the bootstrap credential if we were presenting something else
/// (a stale resumption token). Returns `true` if a fallback was applied; `false` if we were already
/// presenting bootstrap (so the credential itself is bad and the caller should keep backing off).
pub(crate) fn fall_back_to_bootstrap(store: &Arc<Mutex<String>>, bootstrap: &str) -> bool {
    match store.lock() {
        Ok(mut cur) if *cur != bootstrap => {
            *cur = bootstrap.to_string();
            true
        }
        _ => false,
    }
}

/// Things we send up to the server.
pub enum Outgoing {
    Audio(Vec<u8>),       // binary PCM frame (16 kHz mono 16-bit LE)
    Control(ClientMsg),   // JSON control message
}

/// Connection phase, for the trust-UX status pill (B3). The `degraded` AtomicBool drives the
/// pump's fail-open passthrough independently; this is the *user-visible* state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConnPhase {
    /// Initial connect attempt — never been ready yet (audio passes through untranslated).
    Connecting,
    /// Connected + ready: translating normally.
    Ready,
    /// Was ready, the socket dropped, now retrying with backoff. Fail-open passthrough is active,
    /// so the call stays audible but UNTRANSLATED.
    Reconnecting,
}

/// Things the server hands back to the app layer.
pub enum Incoming {
    Ready(String),
    Transcript {
        original: String,
        translated: String,
        latency_ms: Option<f64>,
    },
    Audio(Vec<u8>),       // binary TTS bytes (rate read from the WAV header in audio::play_tts_frame)
    /// Connection phase changed (emitted on transitions only) — drives the status pill.
    Conn(ConnPhase),
    Error(String),
    Other(String),
}

/// Why a single connection ended — drives the reconnect cadence.
enum ExitReason {
    /// Stop requested or the outgoing channel closed — leave the reconnect loop.
    Shutdown,
    /// Connection dropped/failed — reconnect after the normal backoff.
    Transient,
    /// Server reported NOT_READY / MAX_SESSIONS — reconnect, but no faster than the backpressure floor.
    Backpressure,
    /// Server rejected the auth token (AUTH_FAILED) — usually a stale/expired resumption token.
    /// The loop falls back to the bootstrap credential before retrying.
    AuthFailed,
}

struct Outcome {
    reason: ExitReason,
    /// Whether this connection ever reached `ready` (a healthy session), so the loop can reset
    /// the backoff: a long-lived call followed by a blip should retry fast, not at the capped delay.
    reached_ready: bool,
}

/// Supervised connect loop: keeps a live `/ws/translate` session up across drops, backing off
/// between attempts and re-reading the (possibly rotated) auth token each time. Returns only when
/// `stop` is set or the outgoing channel closes. Owns `out_rx` across reconnects.
#[allow(clippy::too_many_arguments)]
pub async fn run_with_reconnect(
    url: String,
    source: String,
    target: String,
    token: Arc<Mutex<String>>,
    bootstrap: String,
    mut out_rx: mpsc::Receiver<Outgoing>,
    in_tx: mpsc::Sender<Incoming>,
    stop: Arc<AtomicBool>,
    degraded: Arc<AtomicBool>,
) {
    let mut backoff = MIN_BACKOFF;
    let mut ever_ready = false;
    let mut last_phase: Option<ConnPhase> = None;
    while !stop.load(Ordering::SeqCst) {
        // Entering a (re)connect: we are not translating until `ready`. Raise the fail-open flag so
        // the pump passes audio through untranslated, and label the phase for the pill — Connecting
        // until we've ever been ready, Reconnecting after. Emit on transition only (no spam).
        degraded.store(true, Ordering::SeqCst);
        let phase = if ever_ready { ConnPhase::Reconnecting } else { ConnPhase::Connecting };
        if last_phase != Some(phase) {
            let _ = in_tx.send(Incoming::Conn(phase)).await;
            last_phase = Some(phase);
        }

        let outcome =
            connect_once(&url, &source, &target, &token, &mut out_rx, &in_tx, &stop, &degraded).await;

        if outcome.reached_ready {
            ever_ready = true;
            last_phase = Some(ConnPhase::Ready); // connect_once emitted Conn(Ready)
            backoff = MIN_BACKOFF; // healthy session → next blip retries promptly
        }
        match outcome.reason {
            ExitReason::Shutdown => break,
            ExitReason::Backpressure => {
                tokio::time::sleep(backoff.max(BACKPRESSURE_BACKOFF)).await;
                backoff = next_backoff(backoff);
            }
            ExitReason::AuthFailed => {
                // A rejected token is almost always a stale/expired resumption token. Fall back to
                // the bootstrap credential for the next attempt; if we *already* presented bootstrap,
                // the credential itself is bad — keep backing off (and surfacing the error) instead
                // of spinning. ever_ready stays true so the pill keeps reading "reconnecting".
                fall_back_to_bootstrap(&token, &bootstrap);
                tokio::time::sleep(backoff).await;
                backoff = next_backoff(backoff);
            }
            ExitReason::Transient => {
                tokio::time::sleep(backoff).await;
                backoff = next_backoff(backoff);
            }
        }
    }
    // Once we stop driving the socket, stay fail-open (the pump is also stopping via `stop`).
    degraded.store(true, Ordering::SeqCst);
}

/// Connect, send `init` with the current token, then pump frames until the socket closes, the
/// outgoing channel ends, or `stop` is set. Updates `token`/`degraded` as `ready` frames arrive.
#[allow(clippy::too_many_arguments)]
async fn connect_once(
    url: &str,
    source: &str,
    target: &str,
    token: &Arc<Mutex<String>>,
    out_rx: &mut mpsc::Receiver<Outgoing>,
    in_tx: &mpsc::Sender<Incoming>,
    stop: &Arc<AtomicBool>,
    degraded: &Arc<AtomicBool>,
) -> Outcome {
    // rustls 0.23 won't auto-select a crypto backend; the wss:// connector needs a
    // process-default CryptoProvider. Idempotent — errs only if one is already set.
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();

    let (stream, _resp) = match connect_async(url).await {
        Ok(s) => s,
        Err(e) => {
            let _ = in_tx.send(Incoming::Error(format!("connect: {e}"))).await;
            return Outcome { reason: ExitReason::Transient, reached_ready: false };
        }
    };
    let (mut write, mut read) = stream.split();

    // Re-read the (possibly rotated) auth token at connect time — the whole point of Option C.
    let auth = token.lock().map(|t| t.clone()).unwrap_or_default();
    let init = ClientMsg::Init {
        auth,
        source_lang: source.to_string(),
        target_lang: target.to_string(),
    };
    let init_txt = serde_json::to_string(&init).unwrap_or_default();
    if write.send(Message::Text(init_txt)).await.is_err() {
        return Outcome { reason: ExitReason::Transient, reached_ready: false };
    }

    let mut reached_ready = false;
    let mut reason = ExitReason::Transient;

    loop {
        if stop.load(Ordering::SeqCst) {
            let _ = write.send(Message::Close(None)).await;
            reason = ExitReason::Shutdown;
            break;
        }

        tokio::select! {
            // Periodic wake so the stop flag is observed even on an idle socket.
            _ = tokio::time::sleep(Duration::from_millis(250)) => {}

            out = out_rx.recv() => match out {
                Some(item) => {
                    let msg = match item {
                        Outgoing::Audio(bytes) => Message::Binary(bytes),
                        Outgoing::Control(c) => match serde_json::to_string(&c) {
                            Ok(s) => Message::Text(s),
                            Err(_) => continue,
                        },
                    };
                    if write.send(msg).await.is_err() {
                        reason = ExitReason::Transient;
                        break;
                    }
                }
                None => {
                    // All senders dropped → app is shutting down.
                    reason = ExitReason::Shutdown;
                    break;
                }
            },

            msg = read.next() => match msg {
                Some(Ok(Message::Text(txt))) => match serde_json::from_str::<ServerMsg>(&txt) {
                    Ok(ServerMsg::Ready { session_id, resumption_token }) => {
                        apply_resumption_token(token, resumption_token);
                        reached_ready = true;
                        // Connected + ready: translating again. Clear fail-open and notify (on transition).
                        if degraded.swap(false, Ordering::SeqCst) {
                            let _ = in_tx.send(Incoming::Conn(ConnPhase::Ready)).await;
                        }
                        let _ = in_tx.send(Incoming::Ready(session_id)).await;
                    }
                    Ok(ServerMsg::Transcript { original, translated, latency_ms, .. }) => {
                        let _ = in_tx
                            .send(Incoming::Transcript { original, translated, latency_ms })
                            .await;
                    }
                    Ok(ServerMsg::Error { code, message }) => {
                        // NOT_READY / MAX_SESSIONS: the node is warming or full — back off harder.
                        // AUTH_FAILED: the token was rejected — the loop will fall back to bootstrap.
                        // The server closes after sending an error, so we stop driving this socket.
                        reason = match code.as_str() {
                            "NOT_READY" | "MAX_SESSIONS" => ExitReason::Backpressure,
                            "AUTH_FAILED" => ExitReason::AuthFailed,
                            _ => ExitReason::Transient,
                        };
                        let _ = in_tx
                            .send(Incoming::Error(format!("{code}: {}", message.unwrap_or_default())))
                            .await;
                        break;
                    }
                    Ok(other) => {
                        let _ = in_tx.send(Incoming::Other(format!("{other:?}"))).await;
                    }
                    Err(_) => {
                        let _ = in_tx.send(Incoming::Other(txt)).await;
                    }
                },
                Some(Ok(Message::Binary(bytes))) => {
                    let _ = in_tx.send(Incoming::Audio(bytes)).await;
                }
                Some(Ok(Message::Close(_))) => break,
                Some(Ok(_)) => {}
                Some(Err(_)) => {
                    reason = ExitReason::Transient;
                    break;
                }
                None => break,
            },
        }
    }

    Outcome { reason, reached_ready }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_starts_low_doubles_and_caps() {
        let mut b = MIN_BACKOFF;
        assert_eq!(b, Duration::from_millis(500));
        b = next_backoff(b);
        assert_eq!(b, Duration::from_secs(1));
        b = next_backoff(b);
        assert_eq!(b, Duration::from_secs(2));
        b = next_backoff(b);
        assert_eq!(b, Duration::from_secs(4));
        b = next_backoff(b);
        assert_eq!(b, Duration::from_secs(8));
        b = next_backoff(b);
        assert_eq!(b, Duration::from_secs(8), "capped at MAX_BACKOFF");
    }

    #[test]
    fn resumption_token_rotates_when_present_else_kept() {
        let store = Arc::new(Mutex::new("bootstrap".to_string()));
        // A ready frame carrying a fresh token rotates the stored value.
        apply_resumption_token(&store, Some("rotated-1".into()));
        assert_eq!(*store.lock().unwrap(), "rotated-1");
        // A ready frame WITHOUT a token (legacy static mode) keeps the current one for reuse.
        apply_resumption_token(&store, None);
        assert_eq!(*store.lock().unwrap(), "rotated-1");
        // And rotates again on the next ready.
        apply_resumption_token(&store, Some("rotated-2".into()));
        assert_eq!(*store.lock().unwrap(), "rotated-2");
    }

    #[test]
    fn auth_failure_falls_back_to_bootstrap_once() {
        let store = Arc::new(Mutex::new("stale-resumption".to_string()));
        // Presenting a stale resumption token → fall back to bootstrap, report it happened.
        assert!(fall_back_to_bootstrap(&store, "bootstrap"));
        assert_eq!(*store.lock().unwrap(), "bootstrap");
        // Already on bootstrap → no fallback (the credential itself is bad; keep backing off).
        assert!(!fall_back_to_bootstrap(&store, "bootstrap"));
        assert_eq!(*store.lock().unwrap(), "bootstrap");
    }
}
