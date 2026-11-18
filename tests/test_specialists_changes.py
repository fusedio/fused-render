"""Revert and drafts (PLAN C.3; PRODUCT §6; acceptance U, V) and the panel / learned / memory routes."""
import json

import pytest
from _specialists_conftest import *
from _specialists_conftest import make_specialist, needs_git

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
    # C3 prefix; an item with no diff lines (an old record) falls back to its undo sentence; never a commit
    assert msg.startswith('Undo this change from the latest update: "Reopen, do not retry."\n')
    assert "Put back retry." in msg and "abc1234" not in msg and RUN not in msg
    assert "never restore whole files from git" in msg and "do not mention ids or commits" in msg
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


# ------------------------------------------------------------------- C3 / C4 ---
def test_app_message_prefixes_are_pinned(monkeypatch):
    """C3: the frontend recognises these app-written messages by their first characters."""
    assert changes.REVERT_PREFIX == "Undo this change from the latest update: "
    assert drafts.SEND_DRAFT_PREFIX == "Review and send this draft to "
    assert drafts.SEND_DRAFT.startswith(drafts.SEND_DRAFT_PREFIX)
    s = make_specialist()
    _seed_changes(s)
    monkeypatch.setattr(chat, "state", lambda sid: {"current": "m", "folded": []})
    assert changes.revert(s.id, RUN, "i1")["message"].startswith(changes.REVERT_PREFIX)
    assert drafts.send(s.id, _draft(s))["message"].startswith(drafts.SEND_DRAFT_PREFIX)


def test_sentence_and_plain_text():
    assert changes.sentence("a  b\nc") == "a b c"
    long = changes.sentence("word " * 60)
    assert len(long) <= 160 and long.endswith("…") and not long[:-1].endswith(" ")
    before = "- [m-a001] Retry the sync. {basis=guess; seen=1}"
    after = "- [m-a001] Reopen the canvas; retrying does not help. {basis=verified; seen=2}"
    items = changes.reconcile([{"kind": "Corrected", "text": "Corrected m-a001", "entry": "m-a001",
                                "file": "memory.md", "from": []}], before + "\n", after + "\n")
    assert items[0]["text"] == "Corrected: Reopen the canvas; retrying does not help."
    assert (items[0]["before"], items[0]["after"]) == (before, after)


def test_reconcile_lists_every_changed_entry():
    """QA defect 4: a run raised seen= with no item; the list must still show it, with its exact lines."""
    old = ("# Memory\n- [m-0001] Keep. {basis=guess; seen=1}\n- [m-0002] Seen more. {basis=verified; seen=1}\n"
           "- [m-0003] Gone. {basis=guess; seen=1}\n")
    new = ("# Memory\n- [m-0001] Keep. {basis=guess; seen=1}\n- [m-0002] Seen more. {basis=verified; seen=12}\n"
           "- [m-0004] Fresh. {basis=user-said; seen=1}\n")
    items = changes.reconcile([{"kind": "Learned", "text": "Fresh.", "entry": "m-0004", "file": "memory.md",
                                "from": ["n-1"]}], old, new)
    got = [(it["kind"], it["entry"], it["before"], it["after"]) for it in items]
    assert got == [("Learned", "m-0004", None, "- [m-0004] Fresh. {basis=user-said; seen=1}"),
                   ("Changed", "m-0002", "- [m-0002] Seen more. {basis=verified; seen=1}",
                    "- [m-0002] Seen more. {basis=verified; seen=12}"),
                   ("Changed", "m-0003", "- [m-0003] Gone. {basis=guess; seen=1}", None)]
    assert items[1]["text"].startswith("Updated how often this was seen") and items[2]["text"] == "Removed: Gone."


def test_ignored_and_from_decide_digested():
    v = changes.validate({"items": [{"kind": "Learned", "text": "x", "from": ["n-aa", "r-1"]}],
                          "ignored": [{"note": "n-bb", "why": "dup"}, "n-cc", {"note": "bad"}],
                          "digested": ["n-dd"]})
    assert v["digested"] == ["n-aa", "n-bb", "n-cc"]
    assert v["ignored"] == [{"note": "n-bb", "why": "dup"}, {"note": "n-cc", "why": ""}]


MEM0 = ("# Memory\n- [m-a001] When a sync stalls, retry. {basis=guess; seen=1}\n"
        "- [m-a002] Fonts load from /assets. {basis=verified; seen=3}\n")


def _git_run(monkeypatch):
    """A real home with git history: one nightly run (stubbed side session) that corrects m-a001, raises m-a002's
    seen= with no item, adds m-a003, a skill and a knowledge file. Returns (spec, run record)."""
    import os

    from _specialists_conftest import stub_side

    from fused_render.specialists import history, inbox, nightly
    s = make_specialist()
    with open(os.path.join(s.home, "memory.md"), "w") as fh:
        fh.write(MEM0)
    history.commit(s.id, "memory")
    n1 = inbox.add(s.id, kind="correction", claim="Reopen, do not retry.", basis="verified", entry="m-a001",
                   source={"type": "task", "session_id": "t"})
    n2 = inbox.add(s.id, kind="gap", claim="Staging first.", basis="user-said", source={"type": "user"})

    def fn(call):
        home = call["cwd"]
        with open(os.path.join(home, "memory.md"), "w") as fh:
            fh.write("# Memory\n- [m-a001] When a sync stalls, reopen the canvas. {basis=verified; seen=2}\n"
                     "- [m-a002] Fonts load from /assets. {basis=verified; seen=4}\n"
                     "- [m-a003] Publish to staging first. {basis=user-said; seen=1}\n")
        os.makedirs(os.path.join(home, ".claude/skills/check-sync"), exist_ok=True)
        with open(os.path.join(home, ".claude/skills/check-sync/SKILL.md"), "w") as fh:
            fh.write("---\nname: check-sync\ndescription: x\n---\nbody\n")
        os.makedirs(os.path.join(home, "knowledge"), exist_ok=True)
        with open(os.path.join(home, "knowledge/fonts.md"), "w") as fh:
            fh.write("# Fonts\n")
        out = re.search(r"write (\S+)/changes\.json", call["prompt"]).group(1)
        with open(os.path.join(out, "changes.json"), "w") as fh:
            json.dump({"summary": "s", "items": [
                {"kind": "Corrected", "text": "Sync stalls: reopen, do not retry.", "entry": "m-a001",
                 "file": "memory.md", "from": [n1["id"]]},
                {"kind": "Learned", "text": "Publish to staging first.", "entry": "m-a003", "file": "memory.md",
                 "from": [n2["id"]]},
                {"kind": "New check", "text": "A check for stalled syncs.", "file": ".claude/skills/check-sync/SKILL.md",
                 "from": [n1["id"]]}]}, fh)
        return "ok"
    stub_side(monkeypatch, fn=fn)
    res = nightly.run_job(s.id)
    assert res["status"] == "done", res
    return s, changes.get(s.id, res["run"])


import re  # noqa: E402


@needs_git
def test_revert_names_one_change_and_marks_the_rest(monkeypatch, api):
    import os
    s, rec = _git_run(monkeypatch)
    run = rec["run"]
    by = {it["text"]: it for it in rec["items"]}
    assert set(by) == {"Sync stalls: reopen, do not retry.", "Publish to staging first.",
                       "A check for stalled syncs.", "Added the file knowledge/fonts.md.",
                       "Updated how often this was seen or confirmed: Fonts load from /assets."}
    first = rec["items"][0]
    monkeypatch.setattr(chat, "state", lambda sid: {"current": "main-1", "folded": []})
    code, _h, body = api.post(f"/api/specialists/{s.id}/changes/{run}/{first['id']}/revert", {})
    msg = body["message"]
    assert msg == (
        'Undo this change from the latest update: "Sync stalls: reopen, do not retry."\n\n'
        "In memory.md, replace this exact line:\n"
        "- [m-a001] When a sync stalls, reopen the canvas. {basis=verified; seen=2}\n"
        "with this exact line:\n"
        "- [m-a001] When a sync stalls, retry. {basis=guess; seen=1}\n\n" + changes.REVERT_TAIL)
    skill = next(it for it in rec["items"] if it["kind"] == "New check")
    assert "Delete the folder .claude/skills/check-sync/ (this update created that skill)." in \
        changes.message(rec, skill)
    know = next(it for it in rec["items"] if it.get("file") == "knowledge/fonts.md")
    assert "Delete knowledge/fonts.md (this update created it)." in changes.message(rec, know)
    added = next(it for it in rec["items"] if it.get("entry") == "m-a003")
    assert "In memory.md, delete this exact line:\n- [m-a003] Publish" in changes.message(rec, added)
    # the main chat does it right: only that line changes; every other item still offers Revert
    with open(os.path.join(s.home, "memory.md")) as fh:
        text = fh.read()
    with open(os.path.join(s.home, "memory.md"), "w") as fh:
        fh.write(text.replace(first["after"], first["before"]))
    _agent(monkeypatch, {"main-1": [{"role": "user", "text": msg}, {"role": "assistant", "text": "Done."}]})
    changes.after_turn(s.id, "main-1")
    states = {it["id"]: it["state"] for it in changes.runs(s.id)[0]["items"]}
    assert states.pop(first["id"]) == "reverted" and set(states.values()) == {"active"}
    code, _h, body = api.post(f"/api/specialists/{s.id}/changes/{run}/{first['id']}/revert", {})
    assert code == 409 and body["reason"] == "gone"


@needs_git
def test_whole_file_restore_marks_every_other_item_changed(monkeypatch, api):
    """QA defect 1: the chat restored files from git; the other items must no longer offer Revert."""
    import os
    import shutil
    s, rec = _git_run(monkeypatch)
    monkeypatch.setattr(chat, "state", lambda sid: {"current": "main-1", "folded": []})
    first = rec["items"][0]
    msg = changes.revert(s.id, rec["run"], first["id"])["message"]
    with open(os.path.join(s.home, "memory.md"), "w") as fh:
        fh.write(MEM0)
    shutil.rmtree(os.path.join(s.home, ".claude/skills/check-sync"))
    os.unlink(os.path.join(s.home, "knowledge/fonts.md"))
    _agent(monkeypatch, {"main-1": [{"role": "user", "text": msg}, {"role": "assistant", "text": "Done."}]})
    changes.after_turn(s.id, "main-1")
    code, _h, body = api.get(f"/api/specialists/{s.id}/learned")
    states = {it["id"]: it["state"] for it in body["runs"][0]["items"]}
    assert states.pop(first["id"]) == "reverted" and set(states.values()) == {"changed"}
    assert body["runs"][0]["at"]
    other = rec["items"][1]["id"]
    code, _h, body = api.post(f"/api/specialists/{s.id}/changes/{rec['run']}/{other}/revert", {})
    assert code == 409 and "changed since" in body["error"]
