"""`health.record_visibility`: the page half of the black-window diagnostics
keeps only the known fields, stamps the server's clock/boot/pid, and echoes
one INFO line so the app log reads as a single timeline."""
import json
import logging

from fused_render import health


def test_record_visibility_filters_stamps_and_logs(tmp_path, monkeypatch, caplog):
    path = tmp_path / "visibility.jsonl"
    monkeypatch.setattr(health, "visibility_path", lambda: str(path))
    with caplog.at_level(logging.INFO, logger="fused_render.health"):
        row = health.record_visibility({"ev": "raf-stalled", "page": "/tasks", "state": "visible",
                                        "hasFocus": True, "rafGap": 2400, "sinceRaf": 2400,
                                        "t": 1.5, "junk": "dropped"})
    assert "junk" not in row and row["kind"] == "visibility"
    assert row["ev"] == "raf-stalled" and row["pid_now"] and row["boot_id_now"]
    on_disk = json.loads(path.read_text().strip().splitlines()[-1])
    assert on_disk["page"] == "/tasks" and "junk" not in on_disk
    assert any("page visibility raf-stalled: page=/tasks state=visible hasFocus=True rafGap=2400"
               in r.getMessage() for r in caplog.records)
