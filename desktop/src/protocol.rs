//! Wire protocol for `/ws/translate` (see docs/STAGE2_DESIGN.md §5.1).
//! Control + results are JSON text frames; audio (both directions) is binary frames.

use serde::{Deserialize, Serialize};

/// Messages the client SENDS (serialized to JSON text frames).
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientMsg {
    Init {
        auth: String,
        source_lang: String,
        target_lang: String,
    },
    EndOfStream,
    Ping { ts: u64 },
    RedactStart,
    RedactEnd,
}

/// Messages the client RECEIVES as JSON text frames.
/// (Binary frames carry TTS audio and are handled outside this enum.)
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerMsg {
    Ready {
        session_id: String,
        /// Single-use HMAC token to present on the next reconnect (Option C — server
        /// mints + rotates one per `ready`). Absent in legacy static-token mode.
        /// docs/decisions/2026-06-20-ws-auth-resumption-token.md
        #[serde(default)]
        resumption_token: Option<String>,
    },
    Transcript {
        original: String,
        translated: String,
        #[serde(default)]
        latency_ms: Option<f64>,
        #[serde(default)]
        stage_ms: Option<serde_json::Value>,
    },
    QueueStatus {
        #[serde(default)]
        stt_depth: Option<u32>,
        #[serde(default)]
        translate_depth: Option<u32>,
        #[serde(default)]
        tts_depth: Option<u32>,
    },
    Redaction {
        active: bool,
    },
    Pong {
        #[serde(default)]
        ts: Option<u64>,
    },
    Error {
        code: String,
        #[serde(default)]
        message: Option<String>,
    },
}
