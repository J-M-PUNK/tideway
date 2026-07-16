"""Running desktop.py from a source checkout without a Vite build used
to open a window on a JSON 404 — a blank white page with nothing
actionable anywhere (#262). The shell must refuse to start and say how
to build the frontend instead.
"""
from __future__ import annotations

import desktop
from app import paths


def test_main_refuses_without_web_dist(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(paths, "bundled_resource_dir", lambda: tmp_path)

    rc = desktop.main([])

    assert rc == 1
    err = capsys.readouterr().err
    assert "web/dist is missing" in err
    assert "npm run build" in err


def test_main_guard_passes_with_web_dist(monkeypatch, tmp_path):
    """With an index.html in place the guard lets main() continue — it
    should get as far as the single-instance probe, which we stub to
    short-circuit the rest of startup."""
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    monkeypatch.setattr(paths, "bundled_resource_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_probe_existing_instance", lambda: True)
    monkeypatch.setattr(desktop, "_ask_existing_to_focus", lambda: None)

    assert desktop.main([]) == 0
