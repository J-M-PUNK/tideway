"""Tests for PCMPlayer.audio_health() — the cumulative playback-health
counters surfaced in the activity report.

The whole point of these counters is remote triage: a stutter bug
report should say *which* of the three failure classes is happening
(driver under/overrun vs our-side queue starvation vs GIL/CPU callback
jitter) without anyone having to fetch the rate-limited audio.log. So
we pin that each diagnostic path bumps its own lifetime total and that
the worst-jitter figure tracks the largest single late delivery.

A bare __new__ instance is enough: the diagnostics only touch the
counter attributes, not the audio stream / threads, so we skip the
heavy real constructor.
"""
from __future__ import annotations

from app.audio.player import PCMPlayer


def _bare_player() -> PCMPlayer:
    p = PCMPlayer.__new__(PCMPlayer)
    p._cb_status_count = 0
    p._cb_status_last_print = 0.0
    p._cb_status_total = 0
    p._cb_starve_count = 0
    p._cb_starve_last_print = 0.0
    p._cb_starve_total = 0
    p._cb_jitter_count = 0
    p._cb_jitter_last_print = 0.0
    p._cb_jitter_worst_ms = 0.0
    p._cb_jitter_total = 0
    p._cb_jitter_worst_late_ms = 0.0
    p._samples_emitted = 0
    return p


def test_health_is_all_zero_before_any_glitch():
    h = _bare_player().audio_health()
    assert h["output_underruns"] == 0
    assert h["queue_starvations"] == 0
    assert h["callback_jitter_events"] == 0
    assert h["worst_jitter_late_ms"] == 0.0
    assert h["pcm_queue_max"] == 100
    # A bare instance has no queue yet — must degrade to None, not raise.
    assert h["pcm_queue_depth"] is None


def test_each_glitch_class_bumps_its_own_total():
    p = _bare_player()
    p._log_callback_status("output_underflow")
    p._log_callback_status("output_underflow")
    p._log_callback_starvation()
    p._log_callback_jitter(gap_s=0.25, expected_s=0.1)  # 150 ms late

    h = p.audio_health()
    assert h["output_underruns"] == 2
    assert h["queue_starvations"] == 1
    assert h["callback_jitter_events"] == 1
    assert h["worst_jitter_late_ms"] == 150.0


def test_worst_jitter_tracks_the_largest_late_delivery():
    p = _bare_player()
    p._log_callback_jitter(gap_s=0.12, expected_s=0.1)  # 20 ms late
    p._log_callback_jitter(gap_s=0.30, expected_s=0.1)  # 200 ms late
    p._log_callback_jitter(gap_s=0.15, expected_s=0.1)  # 50 ms late
    assert p.audio_health()["worst_jitter_late_ms"] == 200.0
    assert p.audio_health()["callback_jitter_events"] == 3
