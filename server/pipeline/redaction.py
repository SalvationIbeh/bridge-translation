"""
Redaction mode — server-side PCI-avoidance enforcement (§8.4).

Council decision 2026-06-14 (docs/decisions/2026-06-14-pci-avoidance.md): Bridge
must never be in the card-data path. An agent triggers a redacted segment via a
control message; the client SHOULD drop audio frames at source, and the server
ALSO drops them — belt-and-suspenders, so a flagged card-entry segment is never
transcribed, translated, synthesized, or logged even if the client misbehaves.

This module holds the small, pure decision/state so it is unit-testable without a
running server or the VAD/STT models. The WebSocket handler owns the side effects
(discarding the VAD buffer, acking the client).
"""


class RedactionGate:
    """Per-session redaction state. `active` gates inbound audio. Counters give
    cheap observability into how often redaction is actually used."""

    def __init__(self) -> None:
        self.active = False
        self.segments = 0          # redacted segments started this session
        self.frames_dropped = 0    # audio frames discarded while active

    def start(self) -> None:
        if not self.active:
            self.segments += 1     # count a segment only on a real OFF→ON edge
        self.active = True

    def end(self) -> None:
        self.active = False

    def should_drop_audio(self) -> bool:
        """Call for each inbound audio frame. Returns True if the frame must be
        dropped (redaction active) and records the drop."""
        if self.active:
            self.frames_dropped += 1
            return True
        return False
