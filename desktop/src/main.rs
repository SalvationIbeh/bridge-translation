//! P1a headless probe: mic → /ws/translate → playback. Proves the live loop and the
//! protocol contract before the Tauri UI (P1b) wraps the same core.

use anyhow::Result;
use clap::Parser;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;
use vocivio_core::{audio, protocol::ClientMsg, pump, ws};

#[derive(Parser)]
#[command(name = "vocivio-probe", about = "Vocivio desktop core probe")]
struct Args {
    /// WebSocket URL of the translation server.
    #[arg(long, default_value = "ws://localhost:8000/ws/translate")]
    server: String,
    /// Auth token (mint via scripts/mint_token.py).
    #[arg(long, default_value = "")]
    token: String,
    #[arg(long, default_value = "es")]
    source: String,
    #[arg(long, default_value = "en")]
    target: String,
    /// Input device name (default host input if omitted). List names with --list-devices.
    #[arg(long)]
    input_device: Option<String>,
    /// Output device name (default host output if omitted).
    #[arg(long)]
    output_device: Option<String>,
    /// Print available audio device names and exit.
    #[arg(long)]
    list_devices: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(std::env::var("RUST_LOG").unwrap_or_else(|_| "info".into()))
        .init();
    let args = Args::parse();

    if args.list_devices {
        let (inputs, outputs) = audio::enumerate_devices()?;
        println!("Inputs:\n{}", inputs.iter().map(|n| format!("  {n}")).collect::<Vec<_>>().join("\n"));
        println!("Outputs:\n{}", outputs.iter().map(|n| format!("  {n}")).collect::<Vec<_>>().join("\n"));
        return Ok(());
    }

    let (out_tx, out_rx) = mpsc::channel::<ws::Outgoing>(256);
    let (in_tx, mut in_rx) = mpsc::channel::<ws::Incoming>(256);

    // Shared state across reconnects. `token` is re-read on every connect attempt so a rotated
    // resumption token recovers the session; `degraded` drives fail-open passthrough; `stop` ends
    // the reconnect loop and the pump. The probe has no redaction UI, so that gate stays clear.
    let token = Arc::new(Mutex::new(args.token.clone()));
    let stop = Arc::new(AtomicBool::new(false));
    let degraded = Arc::new(AtomicBool::new(false));
    let redacting = Arc::new(AtomicBool::new(false));
    let kill = Arc::new(AtomicBool::new(false)); // probe has no kill-switch UI

    // Audio engine (kept alive until end). TTS playback rate depends on the target voice.
    let (engine, frame_rx, sink) = audio::start(
        audio::tts_rate_for(&args.target),
        args.input_device.as_deref(),
        args.output_device.as_deref(),
        degraded.clone(),
    )?;

    // Captured mic frames → server as binary PCM (16-bit LE); copied to the sink (untranslated)
    // when degraded (fail-open). The sink is shared with TTS playback below.
    pump::spawn(
        frame_rx,
        out_tx.clone(),
        Some(sink.clone()),
        redacting,
        degraded.clone(),
        kill,
        stop.clone(),
    );

    // Server messages → console + playback.
    tokio::spawn(async move {
        while let Some(inc) = in_rx.recv().await {
            match inc {
                ws::Incoming::Ready(id) => tracing::info!("session ready: {id}"),
                ws::Incoming::Transcript {
                    original,
                    translated,
                    latency_ms,
                } => {
                    println!(
                        "[{:>5.0}ms] {original}  ->  {translated}",
                        latency_ms.unwrap_or(0.0)
                    );
                }
                ws::Incoming::Audio(bytes) => {
                    sink.play_tts_frame(&bytes);
                }
                ws::Incoming::Conn(phase) => match phase {
                    ws::ConnPhase::Connecting => tracing::info!("connecting…"),
                    ws::ConnPhase::Ready => tracing::info!("connection ready — translating"),
                    ws::ConnPhase::Reconnecting => {
                        tracing::warn!("reconnecting — passthrough (UNTRANSLATED)")
                    }
                },
                ws::Incoming::Error(e) => tracing::error!("server error: {e}"),
                ws::Incoming::Other(o) => tracing::debug!("msg: {o}"),
            }
        }
    });

    tokio::select! {
        _ = ws::run_with_reconnect(
            args.server.clone(), args.source.clone(), args.target.clone(),
            token, args.token.clone(), out_rx, in_tx, stop.clone(), degraded.clone(),
        ) => {
            tracing::info!("reconnect loop ended");
        }
        _ = tokio::signal::ctrl_c() => {
            tracing::info!("stopping — sending end_of_stream");
            stop.store(true, Ordering::SeqCst);
            let _ = out_tx.send(ws::Outgoing::Control(ClientMsg::EndOfStream)).await;
        }
    }

    drop(engine);
    Ok(())
}
