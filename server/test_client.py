"""
Quick end-to-end test client for LinguaBridge server.
Streams a PCM audio file through the WebSocket pipeline and saves the translated audio.

Usage:
    python test_client.py [audio_file] [source_lang] [target_lang]

Defaults:
    audio_file  = ../samples/realtime-pcm/card_block.pcm
    source_lang = es
    target_lang = en

The server must be running:
    AUTH_TOKEN=test MODELS_DIR=../models python main.py
"""
import asyncio
import json
import os
import sys
import time
import wave

import websockets


SERVER_URL   = os.getenv("SERVER_URL", "ws://localhost:8000/ws/translate")
AUTH_TOKEN   = os.getenv("AUTH_TOKEN", "test")
CHUNK_MS     = 20
SAMPLE_RATE  = 16000
CHUNK_BYTES  = SAMPLE_RATE * CHUNK_MS // 1000 * 2   # 16-bit = 2 bytes/sample


def load_pcm(path: str) -> bytes:
    """Load raw PCM (16-bit, 16kHz mono) or WAV/MP3 file as raw PCM bytes."""
    if path.endswith(".pcm"):
        with open(path, "rb") as f:
            return f.read()
    # WAV
    with wave.open(path, "rb") as wf:
        return wf.readframes(wf.getnframes())


async def run_test(audio_path: str, source_lang: str, target_lang: str):
    pcm = load_pcm(audio_path)
    print(f"audio:  {audio_path} ({len(pcm)//1024}KB, ~{len(pcm)/SAMPLE_RATE/2:.1f}s)")
    print(f"langs:  {source_lang} -> {target_lang}")
    print(f"server: {SERVER_URL}\n")

    async with websockets.connect(SERVER_URL) as ws:
        # Init handshake
        await ws.send(json.dumps({
            "type":        "init",
            "auth":        AUTH_TOKEN,
            "source_lang": source_lang,
            "target_lang": target_lang,
        }))

        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", f"unexpected: {ready}"
        session_id = ready["session_id"]
        print(f"session: {session_id[:8]}… connected\n")

        # Stream audio in 20ms chunks (real-time pacing)
        t_stream_start = time.perf_counter()
        chunks = [pcm[i:i+CHUNK_BYTES] for i in range(0, len(pcm), CHUNK_BYTES)]
        for chunk in chunks:
            await ws.send(chunk)
            await asyncio.sleep(CHUNK_MS / 1000)   # simulate real-time mic

        elapsed_stream = (time.perf_counter() - t_stream_start) * 1000

        # Signal end of audio so server flushes any buffered VAD utterance
        await ws.send(json.dumps({"type": "end_of_stream"}))
        print(f"streamed {len(chunks)} chunks in {elapsed_stream:.0f}ms\nwaiting for result...\n")

        # Wait for transcript + audio response (with timeout)
        t_wait_start = time.perf_counter()
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=60.0)

                if isinstance(msg, str):
                    frame = json.loads(msg)
                    ftype = frame.get("type")

                    if ftype == "transcript":
                        latency = frame.get("latency_ms", "?")
                        print(f"original:   {frame.get('original')}")
                        print(f"translated: {frame.get('translated')}")
                        print(f"latency:    {latency}ms end-to-end")
                        stages = frame.get("stage_ms", {})
                        if stages:
                            print(f"  ├ queue_wait: {stages.get('queue_wait')}ms")
                            print(f"  ├ stt:        {stages.get('stt')}ms")
                            print(f"  ├ translate:  {stages.get('translate')}ms")
                            print(f"  └ tts:        {stages.get('tts')}ms")

                    elif ftype == "queue_status":
                        depths = {k: v for k, v in frame.items() if k != "type"}
                        print(f"  queue depths: {depths}")

                    elif ftype == "error":
                        print(f"ERROR: {frame}")
                        return

                elif isinstance(msg, bytes):
                    t_total = (time.perf_counter() - t_wait_start) * 1000
                    out_path = "test_output.wav"
                    with open(out_path, "wb") as f:
                        f.write(msg)
                    print(f"\naudio saved -> {out_path} ({len(msg)//1024}KB)")
                    print(f"total wait after streaming: {t_total:.0f}ms")
                    return   # Got what we came for

        except asyncio.TimeoutError:
            print("timed out waiting for response — check server logs")


if __name__ == "__main__":
    audio  = sys.argv[1] if len(sys.argv) > 1 else "../samples/realtime-pcm/card_block.pcm"
    src    = sys.argv[2] if len(sys.argv) > 2 else "es"
    tgt    = sys.argv[3] if len(sys.argv) > 3 else "en"
    asyncio.run(run_test(audio, src, tgt))
