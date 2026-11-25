"""
Unit tests for redaction-mode enforcement (§8.4 PCI-avoidance). No GPU, no server:

    python server/test_redaction.py

Validates the RedactionGate state machine: inactive passes audio, active drops it,
segment counting on OFF->ON edges only, and frame-drop accounting.
"""
from __future__ import annotations

import sys

from pipeline.redaction import RedactionGate


def test_inactive_passes_audio() -> None:
    g = RedactionGate()
    assert g.active is False
    assert g.should_drop_audio() is False
    assert g.frames_dropped == 0
    assert g.segments == 0


def test_active_drops_audio() -> None:
    g = RedactionGate()
    g.start()
    assert g.active is True
    assert g.segments == 1
    assert g.should_drop_audio() is True
    assert g.should_drop_audio() is True
    assert g.frames_dropped == 2


def test_segment_counts_on_edge_only() -> None:
    g = RedactionGate()
    g.start()
    g.start()          # already active — not a new segment
    assert g.segments == 1
    g.end()
    assert g.active is False
    assert g.should_drop_audio() is False   # audio flows again after end
    g.start()          # re-entry → new segment
    assert g.segments == 2


def test_end_is_idempotent() -> None:
    g = RedactionGate()
    g.end()            # end with nothing active — no crash, stays inactive
    assert g.active is False
    assert g.segments == 0


def main() -> int:
    test_inactive_passes_audio()
    test_active_drops_audio()
    test_segment_counts_on_edge_only()
    test_end_is_idempotent()
    print("ALL REDACTION TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, ".")
    raise SystemExit(main())
