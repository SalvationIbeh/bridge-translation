"""
gen_clips_pod.py — generate Spanish speech clips ON the pod using the baked Piper
voice, so the S2 load harness has real audio without transferring 170KB clips over
the proxy PTY. Output: raw 16-bit mono PCM at the voice's native rate (es = 16000 Hz,
which is exactly the server's AUDIO_SAMPLE_RATE) + a harness-compatible manifest.

Run on the pod:  python gen_clips_pod.py --out /tmp/clips
"""
import argparse
import io
import json
import os
import wave

UTTERANCES = [
    ("balance_query",    "Hola, buenos días, quisiera consultar el saldo de mi cuenta por favor."),
    ("card_declined",    "Mi tarjeta fue rechazada cuando intenté hacer un pago en línea esta mañana."),
    ("transfer_status",  "¿Pueden confirmar si la transferencia que hice ayer ya fue procesada?"),
    ("block_card",       "Necesito bloquear mi tarjeta de inmediato porque la perdí en el autobús."),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/clips")
    ap.add_argument("--voice", default="/models/tts/es_ES-mls_10246-low.onnx")
    args = ap.parse_args()

    from piper import PiperVoice
    voice = PiperVoice.load(args.voice, use_cuda=False)  # CPU: one-time, keep GPU free
    os.makedirs(args.out, exist_ok=True)

    cases = []
    for cid, text in UTTERANCES:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(voice.config.sample_rate)
            voice.synthesize(text, wf)
        wav = buf.getvalue()
        if len(wav) <= 44:
            print(f"WARN: empty audio for {cid!r}")
            continue
        pcm = wav[44:]  # strip WAV header → raw PCM the harness streams
        with open(os.path.join(args.out, f"{cid}.pcm"), "wb") as f:
            f.write(pcm)
        cases.append({"id": cid, "reference": text})
        print(f"  {cid}: {len(pcm)} bytes @ {voice.config.sample_rate}Hz")

    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"cases": cases}, f, ensure_ascii=False, indent=2)
    print(f"GEN_OK: {len(cases)} clips → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
