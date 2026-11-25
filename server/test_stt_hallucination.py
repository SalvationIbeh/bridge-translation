"""
Regression test for the Whisper non-speech hallucination filter (pipeline.stt_worker).

Origin: the 2026-06-19 live desktop probe produced a full fake conversation from a
SILENT mic — Whisper invented "Subscribe!" / "Subtitles by Amara.org" and fell into
repetition loops on ambient noise. _is_hallucinated() gates those out while keeping
real speech (including short and high-no-speech-prob-but-confident utterances).

Run: python -m pytest server/test_stt_hallucination.py   (or execute directly)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from pipeline.stt_worker import _is_hallucinated  # noqa: E402


# (text, no_speech_prob, avg_logprob, expect_dropped)
CASES = [
    # — invented on non-speech: MUST drop —
    ("Subtítulos por la comunidad de Amara.org", 0.3, -0.5, True),
    ("¡Suscríbete!", 0.4, -0.4, True),
    ("Subscribe to my channel", 0.3, -0.4, True),
    ("Hola. Hola. Hola. Hola. Hola. Hola. Hola. Hola.", 0.2, -0.3, True),  # 1-word loop
    ("Okay. Okay. Okay. Okay. Okay.", 0.2, -0.3, True),                     # 1-word loop (short)
    ("It's okay, it's okay, it's okay.", 0.2, -0.3, True),                  # 2-word phrase loop
    ("", 0.1, -0.2, True),                                                  # empty
    ("   ", 0.1, -0.2, True),                                               # whitespace
    ("algo bajo confianza", 0.7, -1.5, True),   # high no-speech AND low confidence
    # — real speech: MUST keep —
    ("¿Cómo estás?", 0.1, -0.25, False),
    ("Pero a la hora, en la primera hora, no estaba enfocada.", 0.2, -0.4, False),
    ("3", 0.2, -0.3, False),                                  # short but real
    ("algo claro con ruido de fondo", 0.7, -0.5, False),     # high no-speech but confident
    ("Hello, my name is Salvation.", 0.2, -0.3, False),      # real (probe sample)
    ("No, no es así, es diferente.", 0.1, -0.3, False),      # repeated word, not a full cycle
]


def test_hallucination_filter():
    failures = []
    for text, ns, lp, expect in CASES:
        got = _is_hallucinated(text, ns, lp)
        if got != expect:
            failures.append(f"{text!r}: got drop={got}, expected {expect}")
    assert not failures, "filter mismatches:\n" + "\n".join(failures)


if __name__ == "__main__":
    test_hallucination_filter()
    print(f"PASS — {len(CASES)} cases")
