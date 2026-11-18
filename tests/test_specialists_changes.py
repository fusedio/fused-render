"""Revert and drafts (PLAN C.3; PRODUCT §6; acceptance U, V) and the panel / learned / memory routes."""
import json

import pytest
from _specialists_conftest import *  # noqa: F401,F403
from _specialists_conftest import make_specialist

from fused_render.specialists import changes, chat, drafts, services

RUN = "20261118-030000-abcd"

class _Api:
    """`client` with JSON bodies decoded."""

    def __init__(self, c):
        self.c = c

    @staticmethod
    def _w(r):
        code, h, b = r
        return code, h, (json.loads(b) if b else None)

    def get(self, p):
        return self._w(self.c.get(p))

    def post(self, p, b):
        return self._w(self.c.post(p, b))

    def delete(self, p):
        return self._w(self.c.delete(p))


@pytest.fixture
def api(client):
    return _Api(client)



def _seed_changes(s):
    return changes.save(s.id, RUN, summary="Read 2 notes.", commit="abc1234", row="row1", status="done",
                        fallback=False, drafts=[],
                        items=[{"kind": "Learned", "text": "Publish to staging first.", "entry": "m-a1b2",
                                "file": "memory.md", "from": ["n-1"], "undo": "Remove m-a1b2."},
                               {"kind": "Corrected", "text": "Reopen, do not retry.", "entry": "m-3f2a",
                                "file": "memory.md", "from": ["n-2"], "undo": "Put back retry."}])


class _Agent:
    def __init__(self, turns):
        self.turns = turns

    def _history(self, file, sid, **kw):
        return {"turns": self.turns.get(sid, [])}


def _agent(monkeypatch, turns):
    import fused_render.claude_agent as ca
    monkeypatch.setattr(ca, "agent_module", lambda: _Agent(turns))


def test_revert_message_and_state(monkeypatch, api):
    s = make_specialist()
    _seed_changes(s)
    monkeypatch.setattr(chat, "state", lambda sid: {"current": "main-1", "folded": []})
    code, _h, body = api.post(f"/api/specialists/{s.id}/changes/{RUN}/i2/revert", {})
    assert code == 200 and body["session_id"] == "main-1"
    msg = body["message"]
    assert msg.startswith("Revert last night's change: Corrected, \"Reopen, do not retry.\"")
    assert f"(nightly update {RUN}, commit abc1234; how to undo: Put back retry.)" in msg
    assert "keep the notes it came from" in msg
    code, _h, body = api.get(f"/api/specialists/{s.id}/learned")
    states = {it["id"]: it["state"] for it in body["runs"][0]["items"]}
    assert states == {"i1": "active", "i2": "reverting"}
    # an earlier turn ending (the message is not in the conversation yet) changes nothing
    _agent(monkeypatch, {"main-1": [{"role": "user", "text": "hello"}, {"role": "assistant", "text": "hi"}]})
    changes.after_turn(s.id, "main-1")
    assert changes.runs(s.id)[0]["items"][1]["state"] == "reverting"
    # the turn that carried it ends: Reverted; the OTHER item is untouched
    _agent(monkeypatch, {"main-1": [{"role": "user", "text": msg},
                                    {"role": "assistant", "text": "I removed the change."}]})
    changes.after_turn(s.id, "main-1")
    items = changes.runs(s.id)[0]["items"]
    assert [it["state"] for it in items] == ["active", "reverted"]
    # unknown item
    code, _h, _b = api.post(f"/api/specialists/{s.id}/changes/{RUN}/i9/revert", {})
    assert code == 404


def test_revert_without_main_session_binds_to_next(monkeypatch):
    s = make_specialist()
    _seed_changes(s)
    monkeypatch.setattr(chat, "state", lambda sid: {"current": None, "folded": []})
    out = changes.revert(s.id, RUN, "i1")
    assert out["session_id"] is None
    monkeypatch.setattr(chat, "state", lambda sid: {"current": "new-1", "folded": []})
    _agent(monkeypatch, {"new-1": [{"role": "user", "text": out["message"]}, {"role": "assistant", "text": "ok"}]})
    changes.after_turn(s.id, "new-1")
    assert changes.runs(s.id)[0]["items"][0]["state"] == "reverted"


def _draft(s):
    return drafts.materialize(s.id, RUN, [{"to": {"name": "Ana", "service": "slack", "handle": "#canvas"},
                                           "subject": "Sync bug", "text": "Hi Ana, sync stalls.", "why": "x"}])[0]


def test_draft_waits_send_raises_card_and_settles(monkeypatch, api):
    s = make_specialist()
    did = _draft(s)
    assert _draft(s) == did  # idempotent
    code, _h, body = api.get(f"/api/specialists/{s.id}/drafts")
    assert [d["id"] for d in body["drafts"]] == [did]
    monkeypatch.setattr(chat, "state", lambda sid: {"current": "main-1", "folded": []})
    code, _h, body = api.post(f"/api/specialists/{s.id}/drafts/{did}/send", {})
    assert code == 200 and body["session_id"] == "main-1"
    msg = body["message"]
    assert msg.startswith("Review and send this draft to Ana through slack (#canvas).")
    assert "I will approve the send" in msg and msg.endswith("Subject: Sync bug\n\nHi Ana, sync stalls.")
    assert drafts.send_pending(s.id, "main-1") and not drafts.send_pending(s.id, "other")
    # a turn without a successful service write: waits again
    _agent(monkeypatch, {"main-1": [{"role": "user", "text": msg},
                                    {"role": "assistant", "text": "Here is the text.", "segments": [
                                        {"kind": "text", "text": "Here is the text."}]}]})
    drafts.after_turn(s.id, "main-1")
    assert drafts.get(s.id, did)["status"] == "waiting" and not drafts.send_pending(s.id, "main-1")
    # sent through a service write tool
    drafts.send(s.id, did)
    monkeypatch.setattr(services, "roster", lambda spec, kind: [
        {"name": "slack_post_message", "description": "[Slack, writes] Post a message"}])
    _agent(monkeypatch, {"main-1": [{"role": "user", "text": msg}, {"role": "assistant", "text": "Sent.", "segments": [
        {"kind": "tool", "name": "mcp__specialist_services__slack_post_message", "status": "ok", "input": {}}]}]})
    drafts.after_turn(s.id, "main-1")
    assert drafts.get(s.id, did)["status"] == "sent" and drafts.waiting(s.id) == []
    code, _h, body = api.post(f"/api/specialists/{s.id}/drafts/{did}/discard", {})
    assert code == 400


def test_read_only_service_call_is_not_a_send(monkeypatch):
    s = make_specialist()
    did = _draft(s)
    monkeypatch.setattr(chat, "state", lambda sid: {"current": "m", "folded": []})
    msg = drafts.send(s.id, did)["message"]
    monkeypatch.setattr(services, "roster", lambda spec, kind: [
        {"name": "slack_search", "description": "[Slack, read-only] Search"}])
    _agent(monkeypatch, {"m": [{"role": "user", "text": msg}, {"role": "assistant", "text": "", "segments": [
        {"kind": "tool", "name": "mcp__specialist_services__slack_search", "status": "ok", "input": {}}]}]})
    drafts.after_turn(s.id, "m")
    assert drafts.get(s.id, did)["status"] == "waiting"


def test_discard_removes_from_waiting(api):
    s = make_specialist()
    did = _draft(s)
    code, _h, body = api.post(f"/api/specialists/{s.id}/drafts/{did}/discard", {})
    assert code == 200 and body == {"ok": True}
    assert drafts.waiting(s.id) == [] and drafts.get(s.id, did)["status"] == "discarded"
    code, _h, body = api.post(f"/api/specialists/{s.id}/drafts/d-000000000000/send", {})
    assert code == 404


def test_panel_and_memory_routes(api, monkeypatch):
    s = make_specialist()
    _seed_changes(s)
    _draft(s)
    with open(s.home + "/memory.md", "w") as fh:
        fh.write("# Memory\n- [m-3f2a] Reopen the canvas. {basis=verified; seen=5; confirmed=2026-11-18; "
                 "from=breakout b-1; retires=never}\n- a raw line\n")
    import os
    os.makedirs(s.home + "/knowledge", exist_ok=True)
    with open(s.home + "/knowledge/sync.md", "w") as fh:
        fh.write("# Sync troubles\nbody\n")
    from fused_render.specialists import breakouts, inbox, receipts
    monkeypatch.setattr(breakouts, "open_now", lambda sid: [
        {"id": "b-1", "parent": {"session_id": "t-1", "title": "Task", "file": "/w"}}])
    monkeypatch.setattr(receipts, "guided_week", lambda sid: 4)
    inbox.add(s.id, kind="gap", claim="x", basis="guess", source={"type": "task", "session_id": "t"})
    code, _h, p = api.get(f"/api/specialists/{s.id}/panel")
    assert code == 200
    assert p["learned"]["run"] == RUN and p["learned"]["total"] == 2 and len(p["learned"]["items"]) == 2
    assert len(p["waiting"]) == 1 and p["inbox"] == {"count": 1}
    assert p["tasks"] == {"helping_now": [{"session_id": "t-1", "title": "Task", "file": "/w", "breakout_id": "b-1"}],
                          "guided_week": 4}
    code, _h, m = api.get(f"/api/specialists/{s.id}/memory")
    e = m["entries"][0]
    assert (e["id"], e["basis"], e["seen"], e["from"]) == ("m-3f2a", "verified", 5, "breakout b-1")
    assert m["entries"][1]["id"] == "" and m["entries"][1]["raw"] == "- a raw line"
    assert m["limit_lines"] == 120 and m["lines"] == 3
    assert m["knowledge"] == [{"title": "Sync troubles", "path": s.home + "/knowledge/sync.md"}]
