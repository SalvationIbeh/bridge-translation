//! Mic-frame pump: drains captured 16 kHz mono i16 frames, serializes them to little-endian
//! bytes, and forwards them to the server as binary WS audio.
//!
//! **Redaction at source (PCI §8.4):** while `redacting` is set, frames are dropped HERE — they
//! never leave the machine. This is the client-side control; the server also enforces redaction
//! independently (defense in depth). The gate is **fail-closed**: any frame pulled while the flag
//! is set is discarded, including audio already buffered in the channel before the toggle, so a
//! card-number lead-in already in flight is not transmitted.
//!
//! **Fail-open passthrough (B2):** while `degraded` is set (the WS is down/reconnecting), frames
//! are NOT sent to the server — instead they're copied straight to the output device via the sink
//! (untranslated) so the call stays audible. Redaction still wins over passthrough: a redacted
//! frame is dropped entirely, never played locally, so card audio has no path even when offline.
//!
//! **Kill switch (B3):** `kill` is an operator-controlled override that forces passthrough exactly
//! like `degraded`, regardless of connection health — one click stops translated audio from leaving
//! the machine while keeping the call up. It's a separate flag from `degraded` (which the reconnect
//! loop owns and clears on `ready`) so the loop can't silently un-kill it.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crossbeam_channel::Receiver;
use tokio::sync::mpsc::Sender;

use crate::audio::AudioSink;
use crate::ws::Outgoing;

/// Spawn the OS thread that forwards mic frames to `out_tx`. Exits when `stop` is set or the frame
/// channel closes. Drops frames at source while `redacting` is set; copies them to `sink`
/// (untranslated passthrough) while `degraded` is set instead of sending to the server. `sink` may
/// be `None` (e.g. a headless caller with no passthrough need — degraded frames are then dropped).
/// Returns the join handle (callers may ignore it — dropping just detaches the thread).
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    frame_rx: Receiver<Vec<i16>>,
    out_tx: Sender<Outgoing>,
    sink: Option<AudioSink>,
    redacting: Arc<AtomicBool>,
    degraded: Arc<AtomicBool>,
    kill: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        for frame in frame_rx.iter() {
            if stop.load(Ordering::SeqCst) {
                break;
            }
            // Fail-closed: never emit audio off the box, nor play it locally, while redacting.
            // Redaction is checked FIRST so it wins over fail-open passthrough and the kill switch.
            if redacting.load(Ordering::SeqCst) {
                continue;
            }
            // Passthrough: WS down (degraded) OR operator kill switch → copy input to output
            // untranslated, never to the server. Kill is a manual override of a healthy connection.
            if kill.load(Ordering::SeqCst) || degraded.load(Ordering::SeqCst) {
                if let Some(s) = &sink {
                    s.play_passthrough(&frame);
                }
                continue;
            }
            let mut bytes = Vec::with_capacity(frame.len() * 2);
            for s in frame {
                bytes.extend_from_slice(&s.to_le_bytes());
            }
            if out_tx.blocking_send(Outgoing::Audio(bytes)).is_err() {
                break;
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossbeam_channel::bounded;

    fn count_audio(recv: &mut tokio::sync::mpsc::Receiver<Outgoing>) -> usize {
        let mut n = 0;
        while let Ok(Outgoing::Audio(_)) = recv.try_recv() {
            n += 1;
        }
        n
    }

    /// Run the pump over `n` frames with the given redaction/degraded/kill state (sink `None`, so
    /// passthrough frames are simply not forwarded) and return how many audio frames reached the
    /// server-bound out channel.
    fn run(n: usize, redacting: bool, degraded: bool, kill: bool) -> usize {
        let (ftx, frx) = bounded::<Vec<i16>>(16);
        let (otx, mut orx) = tokio::sync::mpsc::channel::<Outgoing>(64);
        for _ in 0..n {
            ftx.send(vec![0i16; 320]).unwrap();
        }
        drop(ftx); // close the frame channel so the pump thread finishes
        let h = spawn(
            frx,
            otx,
            None,
            Arc::new(AtomicBool::new(redacting)),
            Arc::new(AtomicBool::new(degraded)),
            Arc::new(AtomicBool::new(kill)),
            Arc::new(AtomicBool::new(false)),
        );
        h.join().unwrap();
        count_audio(&mut orx)
    }

    #[test]
    fn forwards_frames_when_healthy() {
        assert_eq!(run(3, false, false, false), 3);
    }

    #[test]
    fn drops_all_frames_when_redacting() {
        assert_eq!(run(3, true, false, false), 0);
    }

    #[test]
    fn does_not_send_to_server_when_degraded() {
        // Degraded → frames go to passthrough (here: dropped, sink=None), never to the server.
        assert_eq!(run(3, false, true, false), 0);
    }

    #[test]
    fn kill_switch_forces_passthrough_on_healthy_connection() {
        // Connection healthy (degraded=false) but kill engaged → nothing reaches the server.
        assert_eq!(run(3, false, false, true), 0);
    }

    #[test]
    fn redaction_wins_over_passthrough() {
        assert_eq!(run(3, true, true, true), 0);
    }

    #[test]
    fn stop_flag_halts_forwarding() {
        let (ftx, frx) = bounded::<Vec<i16>>(16);
        let (otx, mut orx) = tokio::sync::mpsc::channel::<Outgoing>(64);
        for _ in 0..3 {
            ftx.send(vec![0i16; 320]).unwrap();
        }
        drop(ftx);
        let stop = Arc::new(AtomicBool::new(true)); // already stopped
        let h = spawn(
            frx,
            otx,
            None,
            Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicBool::new(false)),
            stop,
        );
        h.join().unwrap();
        assert_eq!(count_audio(&mut orx), 0);
    }
}
