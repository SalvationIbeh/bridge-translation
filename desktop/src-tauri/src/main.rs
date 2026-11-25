// Vocivio desktop — Tauri UI shell (P1b). Wraps the `vocivio-core` crate.
//
// NOTE: scaffold, not yet compiled/tested.
// cpal's Stream is !Send, so the AudioEngine is built and OWNED on a dedicated session
// thread and never moved. The UI <-> core boundary is: Tauri commands in, Tauri events out,
// channels in between.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod keychain;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use serde::Serialize;
use tauri::{AppHandle, Emitter, State};
use vocivio_core::{audio, protocol::ClientMsg, pump, ws};

#[derive(Default)]
struct AppState {
    running: Mutex<Option<Running>>,
}

struct Running {
    stop: Arc<AtomicBool>,
    control: tokio::sync::mpsc::Sender<ws::Outgoing>,
    redacting: Arc<AtomicBool>,
    #[allow(dead_code)] // owned by the reconnect loop; held here for symmetry / future status reads
    degraded: Arc<AtomicBool>,
    /// Operator kill switch — forces fail-open passthrough (stops sending to the server) over a
    /// healthy connection. Separate from `degraded` so the reconnect loop can't clear it.
    kill: Arc<AtomicBool>,
}

#[derive(Clone, Serialize)]
struct TranscriptEvent {
    original: String,
    translated: String,
    latency_ms: f64,
}

#[derive(Clone, Serialize)]
struct StatusEvent {
    status: String,
    detail: String,
}

fn emit_status(app: &AppHandle, status: &str, detail: &str) {
    let _ = app.emit(
        "status",
        StatusEvent {
            status: status.into(),
            detail: detail.into(),
        },
    );
}

#[derive(Clone, Serialize)]
struct DeviceList {
    inputs: Vec<String>,
    outputs: Vec<String>,
}

#[tauri::command]
fn list_devices() -> Result<DeviceList, String> {
    let (inputs, outputs) = audio::enumerate_devices().map_err(|e| e.to_string())?;
    Ok(DeviceList { inputs, outputs })
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
fn connect(
    app: AppHandle,
    state: State<AppState>,
    server: String,
    token: String,
    source: String,
    target: String,
    input_device: Option<String>,
    output_device: Option<String>,
) -> Result<(), String> {
    let mut guard = state.running.lock().map_err(|e| e.to_string())?;
    if guard.is_some() {
        return Err("already connected".into());
    }

    // Resolve the bootstrap credential (Phase E / Option C). A freshly pasted token becomes the new
    // bootstrap and is saved to the OS keychain; an empty field reuses the stored one. No credential
    // anywhere → refuse to connect. The secret never lives in a settings file or plaintext field.
    let bootstrap = if !token.trim().is_empty() {
        keychain::store(keychain::BOOTSTRAP, token.trim());
        token.trim().to_string()
    } else {
        keychain::load(keychain::BOOTSTRAP).unwrap_or_default()
    };
    if bootstrap.is_empty() {
        return Err("no credential — paste a token to connect (it's saved to the OS keychain)".into());
    }

    let stop = Arc::new(AtomicBool::new(false));
    let redacting = Arc::new(AtomicBool::new(false));
    let degraded = Arc::new(AtomicBool::new(false));
    let kill = Arc::new(AtomicBool::new(false));
    // Auth token shared across reconnects; re-read each attempt and rotated in place from each
    // `ready` frame (Option C). Seed from a cached resumption token if present — a stale one is
    // harmless, the loop falls back to bootstrap on AUTH_FAILED — else start from bootstrap.
    let initial = keychain::load(keychain::RESUMPTION).unwrap_or_else(|| bootstrap.clone());
    let token_store = Arc::new(Mutex::new(initial));
    let token_persist = token_store.clone();
    let bootstrap_loop = bootstrap.clone();
    let (out_tx, out_rx) = tokio::sync::mpsc::channel::<ws::Outgoing>(256);
    let out_tx_audio = out_tx.clone();

    let stop_thread = stop.clone();
    let redacting_pump = redacting.clone();
    let degraded_pump = degraded.clone();
    let degraded_loop = degraded.clone();
    let kill_pump = kill.clone();
    let degraded_audio = degraded.clone();
    let app_thread = app.clone();
    let tts_rate = audio::tts_rate_for(&target);

    std::thread::spawn(move || {
        // Audio engine OWNED on this thread (cpal Stream is !Send). Devices selected by name
        // (Phase C); a stream error (device unplugged) flips `degraded` → fail-open passthrough.
        let (engine, frame_rx, sink) = match audio::start(
            tts_rate,
            input_device.as_deref(),
            output_device.as_deref(),
            degraded_audio,
        ) {
            Ok(t) => t,
            Err(e) => {
                emit_status(&app_thread, "error", &format!("audio: {e}"));
                return;
            }
        };

        // Mic frames -> server (binary PCM, 16-bit LE). Redaction gates at source; when degraded
        // (WS down) frames are copied to the sink untranslated (fail-open passthrough).
        pump::spawn(
            frame_rx,
            out_tx_audio.clone(),
            Some(sink.clone()),
            redacting_pump,
            degraded_pump,
            kill_pump,
            stop_thread.clone(),
        );

        let rt = match tokio::runtime::Builder::new_multi_thread().enable_all().build() {
            Ok(rt) => rt,
            Err(e) => {
                emit_status(&app_thread, "error", &format!("runtime: {e}"));
                return;
            }
        };

        let app_blk = app_thread.clone();
        rt.block_on(async move {
            let (in_tx, mut in_rx) = tokio::sync::mpsc::channel::<ws::Incoming>(256);

            // Server messages -> UI events + playback.
            let app_evt = app_blk.clone();
            tokio::spawn(async move {
                while let Some(inc) = in_rx.recv().await {
                    match inc {
                        // The pill is driven by Conn(phase); the session id isn't surfaced in v1.
                        // Persist the just-rotated resumption token so a quick restart can resume
                        // within its TTL (a stale one falls back to bootstrap).
                        ws::Incoming::Ready(_id) => {
                            if let Ok(t) = token_persist.lock() {
                                keychain::store(keychain::RESUMPTION, &t);
                            }
                        }
                        ws::Incoming::Transcript {
                            original,
                            translated,
                            latency_ms,
                        } => {
                            let _ = app_evt.emit(
                                "transcript",
                                TranscriptEvent {
                                    original,
                                    translated,
                                    latency_ms: latency_ms.unwrap_or(0.0),
                                },
                            );
                        }
                        ws::Incoming::Audio(bytes) => {
                            sink.play_tts_frame(&bytes);
                        }
                        // Status pill (B3). Reconnecting == fail-open passthrough: audible but
                        // UNTRANSLATED. The kill-switch state is layered on top in the UI.
                        ws::Incoming::Conn(phase) => match phase {
                            ws::ConnPhase::Connecting => emit_status(&app_evt, "connecting", ""),
                            ws::ConnPhase::Ready => emit_status(&app_evt, "ready", "Translating"),
                            ws::ConnPhase::Reconnecting => emit_status(
                                &app_evt, "reconnecting", "passthrough — UNTRANSLATED"),
                        },
                        ws::Incoming::Error(e) => emit_status(&app_evt, "error", &e),
                        ws::Incoming::Other(_) => {}
                    }
                }
            });

            emit_status(&app_blk, "connecting", &server);

            // Supervised connect loop: reconnects with backoff, re-reads/rotates the token, and
            // drives the degraded flag for fail-open passthrough. Returns only when `stop` is set.
            ws::run_with_reconnect(
                server, source, target, token_store, bootstrap_loop,
                out_rx, in_tx, stop_thread.clone(), degraded_loop,
            )
            .await;
        });

        drop(engine); // stop audio on this thread
        emit_status(&app_thread, "disconnected", "");
    });

    *guard = Some(Running {
        stop,
        control: out_tx,
        redacting,
        degraded,
        kill,
    });
    Ok(())
}

#[tauri::command]
fn disconnect(state: State<AppState>) -> Result<(), String> {
    let mut guard = state.running.lock().map_err(|e| e.to_string())?;
    if let Some(running) = guard.take() {
        running.stop.store(true, Ordering::SeqCst);
        let _ = running
            .control
            .try_send(ws::Outgoing::Control(ClientMsg::EndOfStream));
        // Explicit Stop ends the session: forget the cached resumption token (it's session-scoped;
        // restart-resume is for crashes, not deliberate stops). The bootstrap credential stays.
        keychain::clear(keychain::RESUMPTION);
    }
    Ok(())
}

#[tauri::command]
fn set_redaction(state: State<AppState>, active: bool) -> Result<(), String> {
    let guard = state.running.lock().map_err(|e| e.to_string())?;
    if let Some(running) = guard.as_ref() {
        // Flip the LOCAL gate first — this is the primary control that stops audio at source.
        // Do it before (and independently of) the control frame so that even if the network send
        // fails, no audio leaves the machine while redaction is on (fail-closed). On disable, send
        // the control frame BEFORE clearing the gate so no audio escapes between the two steps.
        if active {
            running.redacting.store(true, Ordering::SeqCst);
        }
        let msg = if active {
            ClientMsg::RedactStart
        } else {
            ClientMsg::RedactEnd
        };
        let send = running
            .control
            .try_send(ws::Outgoing::Control(msg))
            .map_err(|e| e.to_string());
        if !active {
            running.redacting.store(false, Ordering::SeqCst);
        }
        send?;
    }
    Ok(())
}

#[tauri::command]
fn set_kill(state: State<AppState>, active: bool) -> Result<(), String> {
    // Operator kill switch: force fail-open passthrough (stop sending to the server) over a healthy
    // connection, or release it. Purely local — flipping the pump's gate is the whole mechanism; no
    // server frame is needed since diverted audio simply never reaches the socket.
    let guard = state.running.lock().map_err(|e| e.to_string())?;
    if let Some(running) = guard.as_ref() {
        running.kill.store(active, Ordering::SeqCst);
    }
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            connect,
            disconnect,
            set_redaction,
            set_kill,
            list_devices
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
