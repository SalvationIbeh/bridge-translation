//! Mic capture (→ 16 kHz mono 16-bit PCM, 20 ms frames) and TTS playback, via cpal.
//!
//! Scaffold notes:
//! - Linear resampling is "good enough" for a P1 voice loop; swap for `rubato` if quality needs.
//! - TTS playback rate is per-target-voice (`tts_rate_for`) — confirmed against the pod's Piper
//!   voices; the proper fix is the server advertising `sample_rate` in the transcript frame.

use anyhow::{anyhow, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, Host, SampleFormat, Stream};
use crossbeam_channel::{bounded, Receiver};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

/// Format the server protocol expects on the wire.
pub const SERVER_SAMPLE_RATE: u32 = 16_000;
pub const FRAME_SAMPLES: usize = 320; // 20 ms @ 16 kHz

/// Sane bounds for a TTS sample rate parsed off the wire. The server is trusted-ish, but the
/// rate is still untrusted input — an absurd value would blow up the resampler's buffer math.
pub const MIN_TTS_RATE: u32 = 8_000;
pub const MAX_TTS_RATE: u32 = 48_000;
/// Hard cap on a single TTS audio frame, to avoid OOM from a hostile/buggy server. Generous
/// enough for long utterances (16 MB ≈ 6 min of 22 kHz mono int16) — this is an anti-DoS
/// ceiling, not the server's inbound MAX_FRAME_BYTES (TTS replies legitimately exceed 64 KB).
const MAX_TTS_FRAME_BYTES: usize = 16 * 1024 * 1024;

/// FALLBACK ONLY. The server sends a self-describing WAV container, so the true playback rate is
/// read from the header (`parse_wav_pcm`). This per-language guess is used only if a frame arrives
/// without a recognizable WAV header (e.g. an older server emitting headerless PCM). Rates
/// confirmed on pod 2026-06-19: en_US-lessac-medium = 22050 Hz, es_ES-mls_10246 = 16000 Hz.
pub fn tts_rate_for(target_lang: &str) -> u32 {
    match target_lang {
        "es" => 16_000,
        _ => 22_050, // en_US (and default)
    }
}

/// Parse a canonical PCM WAV blob (as produced by the server's Python `wave` writer):
/// validate RIFF/WAVE magic, read the sample rate at byte offset 24 (LE u32), bounds-check it,
/// strip the 44-byte header, and decode the remainder as int16 LE mono PCM. Returns `None` if the
/// blob isn't a WAV we recognize — the caller then treats it as headerless PCM at a fallback rate.
/// Deliberately a tiny fixed-layout reader, NOT a general WAV parser (smaller attack surface).
/// See docs/decisions/2026-06-20-tts-audio-wire-format.md.
pub fn parse_wav_pcm(bytes: &[u8]) -> Option<(u32, Vec<i16>)> {
    if bytes.len() < 44 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return None;
    }
    let rate = u32::from_le_bytes([bytes[24], bytes[25], bytes[26], bytes[27]]);
    if !(MIN_TTS_RATE..=MAX_TTS_RATE).contains(&rate) {
        return None;
    }
    let pcm_bytes = &bytes[44..];
    if pcm_bytes.len() % 2 != 0 {
        return None; // int16 alignment
    }
    let pcm = pcm_bytes
        .chunks_exact(2)
        .map(|c| i16::from_le_bytes([c[0], c[1]]))
        .collect();
    Some((rate, pcm))
}

/// Keeps the live cpal streams alive. Drop to stop audio.
pub struct AudioEngine {
    _input: Stream,
    _output: Stream,
}

/// Playback sink: feed a server TTS audio frame; it gets decoded (WAV header parsed for the true
/// rate), resampled to the device rate, and queued.
#[derive(Clone)]
pub struct AudioSink {
    buf: Arc<Mutex<VecDeque<f32>>>,
    device_rate: u32,
    fallback_rate: u32,
}

impl AudioSink {
    /// Play one TTS audio frame from the server. The sample rate is read per-utterance from the
    /// WAV header; if the frame isn't a recognizable WAV it's treated as headerless PCM at the
    /// fallback rate (older-server compat). Oversized frames are dropped (anti-OOM). The rate is
    /// never carried across utterances — it's derived fresh from each frame.
    pub fn play_tts_frame(&self, bytes: &[u8]) {
        if bytes.len() > MAX_TTS_FRAME_BYTES {
            tracing::warn!("dropping oversized TTS frame: {} bytes", bytes.len());
            return;
        }
        let (rate, pcm) = match parse_wav_pcm(bytes) {
            Some(parsed) => parsed,
            None => {
                let pcm = bytes
                    .chunks_exact(2)
                    .map(|c| i16::from_le_bytes([c[0], c[1]]))
                    .collect();
                (self.fallback_rate, pcm)
            }
        };
        let f: Vec<f32> = pcm.iter().map(|s| *s as f32 / 32768.0).collect();
        let res = resample_linear(&f, rate, self.device_rate);
        if let Ok(mut b) = self.buf.lock() {
            b.extend(res);
        }
    }

    /// Fail-open passthrough (B2): copy untranslated mic input straight to the output device while
    /// the server is down/degraded, so the call stays audible ("Bridge never drops the call").
    /// Input frames are [`SERVER_SAMPLE_RATE`] mono i16 (already downmixed+resampled by `start`);
    /// the format guard resamples to the device rate and **mutes rather than ever playing at the
    /// wrong rate** (no chipmunk). Zero-persistence: samples are resampled in memory and pushed to
    /// the live playback ring buffer only — never written, recorded, or buffered to disk.
    pub fn play_passthrough(&self, pcm: &[i16]) {
        if let Some(res) = passthrough_samples(pcm, SERVER_SAMPLE_RATE, self.device_rate) {
            if let Ok(mut b) = self.buf.lock() {
                b.extend(res);
            }
        }
    }
}

/// Format guard for fail-open passthrough. Returns the resampled f32 samples, or `None` to **mute**
/// when the rates are unusable — never returns audio at a rate that would chipmunk. Pure, so the
/// guard is unit-testable without a real audio device.
fn passthrough_samples(pcm: &[i16], src_rate: u32, device_rate: u32) -> Option<Vec<f32>> {
    if !(MIN_TTS_RATE..=MAX_TTS_RATE).contains(&src_rate) || device_rate == 0 {
        return None; // mute: rates we can't safely resample
    }
    let f: Vec<f32> = pcm.iter().map(|s| *s as f32 / 32768.0).collect();
    Some(resample_linear(&f, src_rate, device_rate))
}

/// Enumerate the host's input and output device names for the UI picker (Phase C). Names are what
/// `start` selects on, so the picker shows exactly what can be chosen. Devices with unreadable names
/// are skipped rather than failing the whole list.
pub fn enumerate_devices() -> Result<(Vec<String>, Vec<String>)> {
    let host = cpal::default_host();
    let inputs = host.input_devices()?.filter_map(|d| d.name().ok()).collect();
    let outputs = host.output_devices()?.filter_map(|d| d.name().ok()).collect();
    Ok((inputs, outputs))
}

/// Pick an input device by name, falling back to the host default if the name is `None` or no longer
/// present (e.g. the virtual cable was unplugged between enumeration and connect).
fn pick_input(host: &Host, name: Option<&str>) -> Result<Device> {
    if let Some(n) = name {
        if let Ok(devs) = host.input_devices() {
            for d in devs {
                if d.name().map(|dn| dn == n).unwrap_or(false) {
                    return Ok(d);
                }
            }
        }
        tracing::warn!("input device {n:?} not found — falling back to default");
    }
    host.default_input_device()
        .ok_or_else(|| anyhow!("no default input device"))
}

/// Pick an output device by name; falls back to the host default. See [`pick_input`].
fn pick_output(host: &Host, name: Option<&str>) -> Result<Device> {
    if let Some(n) = name {
        if let Ok(devs) = host.output_devices() {
            for d in devs {
                if d.name().map(|dn| dn == n).unwrap_or(false) {
                    return Ok(d);
                }
            }
        }
        tracing::warn!("output device {n:?} not found — falling back to default");
    }
    host.default_output_device()
        .ok_or_else(|| anyhow!("no default output device"))
}

/// Build the cpal error callback. A stream error usually means the device vanished (cable
/// unplugged, headset disconnected) — treat it as a degraded connection so the UI flips to the
/// fail-open UNTRANSLATED state (B2). v1 does NOT hot-swap/recover the stream: the operator
/// reselects the device and reconnects (Stop → Connect). See the Phase C scope note in the plan.
fn make_stream_err(degraded: Arc<AtomicBool>, which: &'static str) -> impl FnMut(cpal::StreamError) {
    move |e: cpal::StreamError| {
        tracing::error!("{which} stream error (device lost?): {e}");
        degraded.store(true, Ordering::SeqCst);
    }
}

/// Naive linear resampler. Replace with `rubato` for production quality.
fn resample_linear(input: &[f32], from: u32, to: u32) -> Vec<f32> {
    if from == to || input.is_empty() {
        return input.to_vec();
    }
    let ratio = to as f64 / from as f64;
    let out_len = ((input.len() as f64) * ratio).round() as usize;
    let mut out = Vec::with_capacity(out_len);
    for i in 0..out_len {
        let src = i as f64 / ratio;
        let idx = src.floor() as usize;
        let frac = (src - idx as f64) as f32;
        let a = input.get(idx).copied().unwrap_or(0.0);
        let b = input.get(idx + 1).copied().unwrap_or(a);
        out.push(a + (b - a) * frac);
    }
    out
}

/// Start capture + playback. Returns the engine (keep alive), a receiver of 320-sample
/// 16 kHz frames to send to the server, and a sink for playing TTS audio back.
///
/// `input_name`/`output_name` select devices by name (Phase C — what the UI picker offers); `None`
/// uses the host default. A name that's no longer present falls back to the default. `degraded` is
/// flipped if a stream errors (device vanished) so the call drops to the fail-open passthrough state.
pub fn start(
    fallback_rate: u32,
    input_name: Option<&str>,
    output_name: Option<&str>,
    degraded: Arc<AtomicBool>,
) -> Result<(AudioEngine, Receiver<Vec<i16>>, AudioSink)> {
    let host = cpal::default_host();

    // ---------- input (mic) ----------
    let in_dev = pick_input(&host, input_name)?;
    tracing::info!("input device: {:?}", in_dev.name().unwrap_or_default());
    let in_cfg = in_dev.default_input_config()?;
    let in_rate = in_cfg.sample_rate().0;
    let in_ch = in_cfg.channels() as usize;
    tracing::info!("input: {in_rate} Hz, {in_ch} ch, {:?}", in_cfg.sample_format());

    let (raw_tx, raw_rx) = bounded::<Vec<f32>>(64);
    let cfg: cpal::StreamConfig = in_cfg.clone().into();
    let input = match in_cfg.sample_format() {
        SampleFormat::F32 => in_dev.build_input_stream(
            &cfg,
            move |data: &[f32], _| {
                let _ = raw_tx.try_send(data.to_vec());
            },
            make_stream_err(degraded.clone(), "input"),
            None,
        )?,
        SampleFormat::I16 => in_dev.build_input_stream(
            &cfg,
            move |data: &[i16], _| {
                let v: Vec<f32> = data.iter().map(|s| *s as f32 / 32768.0).collect();
                let _ = raw_tx.try_send(v);
            },
            make_stream_err(degraded.clone(), "input"),
            None,
        )?,
        other => return Err(anyhow!("unsupported input sample format: {other:?}")),
    };
    input.play()?;

    // worker: downmix → resample to 16 kHz → emit 320-sample i16 frames
    let (frame_tx, frame_rx) = bounded::<Vec<i16>>(64);
    thread::spawn(move || {
        let mut acc: Vec<f32> = Vec::new();
        for chunk in raw_rx.iter() {
            let mono: Vec<f32> = if in_ch <= 1 {
                chunk
            } else {
                chunk
                    .chunks(in_ch)
                    .map(|f| f.iter().sum::<f32>() / in_ch as f32)
                    .collect()
            };
            acc.extend(resample_linear(&mono, in_rate, SERVER_SAMPLE_RATE));
            while acc.len() >= FRAME_SAMPLES {
                let frame: Vec<i16> = acc
                    .drain(..FRAME_SAMPLES)
                    .map(|s| (s.clamp(-1.0, 1.0) * 32767.0) as i16)
                    .collect();
                if frame_tx.send(frame).is_err() {
                    return;
                }
            }
        }
    });

    // ---------- output (speaker) ----------
    let out_dev = pick_output(&host, output_name)?;
    tracing::info!("output device: {:?}", out_dev.name().unwrap_or_default());
    let out_cfg = out_dev.default_output_config()?;
    let out_rate = out_cfg.sample_rate().0;
    let out_ch = out_cfg.channels() as usize;
    tracing::info!("output: {out_rate} Hz, {out_ch} ch, {:?}", out_cfg.sample_format());

    let buf = Arc::new(Mutex::new(VecDeque::<f32>::new()));
    let buf_cb = buf.clone();
    let cfg_out: cpal::StreamConfig = out_cfg.clone().into();
    let output = match out_cfg.sample_format() {
        SampleFormat::F32 => out_dev.build_output_stream(
            &cfg_out,
            move |data: &mut [f32], _| {
                let mut b = buf_cb.lock().unwrap();
                for frame in data.chunks_mut(out_ch) {
                    let s = b.pop_front().unwrap_or(0.0);
                    for x in frame.iter_mut() {
                        *x = s;
                    }
                }
            },
            make_stream_err(degraded.clone(), "output"),
            None,
        )?,
        SampleFormat::I16 => out_dev.build_output_stream(
            &cfg_out,
            move |data: &mut [i16], _| {
                let mut b = buf_cb.lock().unwrap();
                for frame in data.chunks_mut(out_ch) {
                    let s = b.pop_front().unwrap_or(0.0);
                    let v = (s.clamp(-1.0, 1.0) * 32767.0) as i16;
                    for x in frame.iter_mut() {
                        *x = v;
                    }
                }
            },
            make_stream_err(degraded.clone(), "output"),
            None,
        )?,
        other => return Err(anyhow!("unsupported output sample format: {other:?}")),
    };
    output.play()?;

    let sink = AudioSink {
        buf,
        device_rate: out_rate,
        fallback_rate,
    };
    Ok((
        AudioEngine {
            _input: input,
            _output: output,
        },
        frame_rx,
        sink,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a canonical 44-byte PCM WAV header + payload, matching Python `wave`'s layout.
    fn wav(rate: u32, samples: &[i16]) -> Vec<u8> {
        let data: Vec<u8> = samples.iter().flat_map(|s| s.to_le_bytes()).collect();
        let byte_rate = rate * 2; // mono, 16-bit
        let mut b = Vec::with_capacity(44 + data.len());
        b.extend_from_slice(b"RIFF");
        b.extend_from_slice(&((36 + data.len()) as u32).to_le_bytes());
        b.extend_from_slice(b"WAVE");
        b.extend_from_slice(b"fmt ");
        b.extend_from_slice(&16u32.to_le_bytes()); // fmt chunk size
        b.extend_from_slice(&1u16.to_le_bytes()); // PCM
        b.extend_from_slice(&1u16.to_le_bytes()); // mono
        b.extend_from_slice(&rate.to_le_bytes());
        b.extend_from_slice(&byte_rate.to_le_bytes());
        b.extend_from_slice(&2u16.to_le_bytes()); // block align
        b.extend_from_slice(&16u16.to_le_bytes()); // bits/sample
        b.extend_from_slice(b"data");
        b.extend_from_slice(&(data.len() as u32).to_le_bytes());
        b.extend_from_slice(&data);
        b
    }

    #[test]
    fn parses_rate_and_pcm() {
        let samples = [0i16, 100, -100, 32767, -32768];
        let (rate, pcm) = parse_wav_pcm(&wav(22050, &samples)).expect("valid WAV");
        assert_eq!(rate, 22050);
        assert_eq!(pcm, samples);
    }

    #[test]
    fn parses_es_voice_rate() {
        let (rate, _) = parse_wav_pcm(&wav(16000, &[1, 2, 3])).unwrap();
        assert_eq!(rate, 16000);
    }

    #[test]
    fn rejects_bad_magic() {
        let mut blob = wav(22050, &[1, 2, 3]);
        blob[0] = b'X'; // corrupt RIFF
        assert!(parse_wav_pcm(&blob).is_none());
    }

    #[test]
    fn rejects_too_short() {
        assert!(parse_wav_pcm(&[0u8; 10]).is_none());
    }

    #[test]
    fn rejects_out_of_range_rate() {
        assert!(parse_wav_pcm(&wav(192_000, &[1, 2])).is_none()); // above MAX
        assert!(parse_wav_pcm(&wav(4_000, &[1, 2])).is_none()); // below MIN
    }

    #[test]
    fn rejects_odd_pcm_length() {
        let mut blob = wav(22050, &[1, 2, 3]);
        blob.push(0xAB); // make payload odd → misaligned int16
        assert!(parse_wav_pcm(&blob).is_none());
    }

    #[test]
    fn passthrough_resamples_and_guards_rate() {
        let pcm = [0i16, 16384, -16384, 32767, -1, 1];
        // Same rate → sample count preserved (no resampling).
        assert_eq!(
            passthrough_samples(&pcm, 16_000, 16_000).unwrap().len(),
            pcm.len()
        );
        // Upsample → strictly more samples.
        assert!(passthrough_samples(&pcm, 16_000, 48_000).unwrap().len() > pcm.len());
        // Out-of-range source rate or a zero device rate → MUTE, never wrong-rate playback.
        assert!(passthrough_samples(&pcm, 1_000, 48_000).is_none());
        assert!(passthrough_samples(&pcm, 96_000, 48_000).is_none());
        assert!(passthrough_samples(&pcm, 16_000, 0).is_none());
    }
}
