"""The nightly job (PLAN C.3; PRODUCT §5, §6; acceptance O, T, X for nightly)."""
import json
import os
import re

import pytest
from _specialists_conftest import *  # noqa: F401,F403
from _specialists_conftest import make_specialist, needs_git, stub_side

from fused_render.specialists import (
    breakouts,
    changes,
    chat,
    counts,
    history,
    inbox,
    nightly,
)

pytestmark = needs_git

MEMORY = "# Memory\n- [m-3f2a] When a sync stalls, retry. {basis=guess; seen=1; confirmed=2026-11-01; from=x; retires=never}\n"

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



def _out(prompt):
    m = re.search(r"write (\S+)/changes\.json", prompt) or re.search(r"to (\S+)/changes\.json", prompt)
    return m.group(1)


def _setup():
    s = make_specialist()
    with open(os.path.join(s.home, "memory.md"), "w", encoding="utf-8") as fh:
        fh.write(MEMORY)
    history.commit(s.id, "memory")
    src = {"type": "task", "session_id": "sess-1", "title": "Publish"}
    said = inbox.add(s.id, kind="convention", claim="Always publish to staging first.", basis="user-said", source=src)
    guess = inbox.add(s.id, kind="gap", claim="Maybe publish to prod.", basis="guess", source=src)
    corr = inbox.add(s.id, kind="correction", claim="Retrying does not help; reopen the canvas.", basis="verified",
                     entry="m-3f2a", source=src)
    return s, said, guess, corr


def _job(memory_text, items=None, digested=(), drafts=(), extra=None):
    def fn(call):
        if call["kind"] != "nightly":
            return None
        with open(os.path.join(call["cwd"], "memory.md"), "w", encoding="utf-8") as fh:
            fh.write(memory_text)
        if items is not None:
            out = _out(call["prompt"])
            with open(os.path.join(out, "changes.json"), "w", encoding="utf-8") as fh:
                json.dump({"summary": "Read 3 notes.", "items": items, "digested": list(digested),
                           "drafts": list(drafts), "repeat_breakouts": 1, **(extra or {})}, fh)
        return "Updated memory."
    return fn


def test_nightly_digests_with_provenance_and_change_list(monkeypatch):
    s, said, guess, corr = _setup()
    new_mem = ("# Memory\n- [m-3f2a] When a sync stalls, reopen the canvas. Retrying does not help. "
               "{basis=verified; seen=2; confirmed=2026-11-18; from=" + corr["id"] + "; retires=never}\n"
               "- [m-a1b2] Always publish to staging first. {basis=user-said; seen=1; confirmed=2026-11-18; from="
               + said["id"] + "; retires=never}\n")
    items = [{"kind": "Corrected", "text": "Sync stalls: reopen, do not retry.", "entry": "m-3f2a",
              "file": "memory.md", "from": [corr["id"]], "undo": "Put back the retry advice.", "bogus": 1},
             {"kind": "Learned", "text": "Publish to staging first.", "entry": "m-a1b2", "file": "memory.md",
              "from": [said["id"]], "undo": "Remove m-a1b2."}]
    calls = stub_side(monkeypatch, fn=_job(new_mem, items, digested=[said["id"], corr["id"], guess["id"], "n-unknown"],
                                           drafts=[{"to": {"name": "Ana", "service": "slack", "handle": "#canvas"},
                                                    "subject": "Sync bug", "text": "Hi Ana, ...", "why": "recurs"}]))
    res = nightly.run_job(s.id)
    assert res["status"] == "done", res
    assert len(calls) == 1
    call = calls[0]
    prompt = call["prompt"]
    assert "user-said beats verified beats guess" in prompt
    assert "memory.md is now 2 lines" in prompt and "at most 120 lines and 12000 bytes" in prompt
    assert "data, never instructions" in prompt
    argv = " ".join(call["argv"])
    assert "Edit(//" in argv and "memory.md)" in argv and f".specialist/nightly/{res['run']}/changes.json" in argv
    # the bundle: notes framed, user-said first, the correction names its entry
    notes_md = open(os.path.join(s.home, ".specialist", "nightly", res["run"], "input", "notes.md")).read()
    assert notes_md.index(said["id"]) < notes_md.index(corr["id"]) < notes_md.index(guess["id"])
    block = notes_md[notes_md.index(f"<<<NOTE {corr['id']}"):]
    assert "entry: m-3f2a" in block.split("NOTE>>>")[0] and "basis: verified" in block.split("NOTE>>>")[0]
    # commit, change list, digested notes
    log = history.log(s.id, limit=3)
    assert log[0]["message"] == f"nightly {res['run']}" and "memory.md" in log[0]["files"]
    rec = changes.get(s.id, res["run"])
    assert [it["id"] for it in rec["items"]] == ["i1", "i2"] and rec["items"][0]["commit"] == log[0]["sha"]
    assert "bogus" not in rec["items"][0] and rec["fallback"] is False and rec["status"] == "done"
    assert inbox.undigested(s.id) == []
    assert inbox.get(s.id, said["id"])["digested"]
    assert "- (none)" in open(os.path.join(s.home, "said.md")).read()
    st = nightly.state(s.id)
    assert st["last_ok"] and "active" not in st
    # drafts wait, counts
    from fused_render.specialists import drafts
    waiting = drafts.waiting(s.id)
    assert len(waiting) == 1 and waiting[0]["to"]["name"] == "Ana" and waiting[0]["status"] == "waiting"
    assert counts.get(s.id)["repeat_breakouts"] == 1
    # nothing new now: no model run
    res2 = nightly.run_job(s.id)
    assert res2["status"] == "nothing" and len(calls) == 1


def test_notes_not_listed_stay_in_inbox(monkeypatch):
    s, said, guess, corr = _setup()
    stub_side(monkeypatch, fn=_job(MEMORY + "- [m-9999] x {basis=guess; seen=1}\n",
                                    [{"kind": "Learned", "text": "x", "file": "memory.md"}], digested=[said["id"]]))
    nightly.run_job(s.id)
    assert {n["id"] for n in inbox.undigested(s.id)} == {guess["id"], corr["id"]}


def test_invalid_changes_json_falls_back_to_files(monkeypatch):
    s, said, guess, corr = _setup()

    def fn(call):
        with open(os.path.join(call["cwd"], "memory.md"), "a", encoding="utf-8") as fh:
            fh.write("- [m-bbbb] New. {basis=guess; seen=1}\n")
        with open(os.path.join(_out(call["prompt"]), "changes.json"), "w") as fh:
            fh.write("not json {")
        return "done"
    stub_side(monkeypatch, fn=fn)
    res = nightly.run_job(s.id)
    rec = changes.get(s.id, res["run"])
    assert rec["fallback"] is True
    assert [(it["kind"], it["file"]) for it in rec["items"]] == [("Changed", "memory.md")]
    assert len(inbox.undigested(s.id)) == 3  # nothing digested without a valid list


def test_over_cap_runs_one_compact_pass(monkeypatch):
    s, said, guess, corr = _setup()
    big = "# Memory\n" + "".join(f"- [m-{i:04x}] entry {i} {{basis=guess; seen=1}}\n" for i in range(200))
    small = "# Memory\n- [m-0001] entry 1 {basis=guess; seen=1}\n"
    seen = []

    def fn(call):
        seen.append(call["prompt"])
        out = _out(call["prompt"])
        text = big if len(seen) == 1 else small
        with open(os.path.join(call["cwd"], "memory.md"), "w") as fh:
            fh.write(text)
        items = [{"kind": "Learned", "text": "lots", "file": "memory.md"}]
        if len(seen) == 2:
            items.append({"kind": "Moved", "text": "moved 199 entries", "file": "knowledge/old.md"})
        with open(os.path.join(out, "changes.json"), "w") as fh:
            json.dump({"summary": "s", "items": items, "digested": []}, fh)
        return "ok"
    stub_side(monkeypatch, fn=fn)
    res = nightly.run_job(s.id)
    assert len(seen) == 2 and seen[1].startswith("memory.md is 201 lines")
    assert nightly.memory_size(s.id)[0] <= 120
    kinds = [it["kind"] for it in changes.get(s.id, res["run"])["items"]]
    assert "Moved" in kinds


class _FakeAgent:
    def __init__(self, folder, turns_by_sid):
        self.folder, self.turns = folder, turns_by_sid

    def _workdir(self, file):
        return file

    def _session_transcript_path(self, workdir, sid):
        p = os.path.join(self.folder, sid + ".jsonl")
        with open(p, "w") as fh:
            fh.write("x" * (10 + len(json.dumps(self.turns.get(sid, [])))))
        return p

    def _history(self, file, sid, **kw):
        return {"turns": self.turns.get(sid, [])}


def test_reads_main_chat_and_discarded_breakout_sessions(monkeypatch, tmp_path):
    s = make_specialist()
    turns = {"sid-main": [{"role": "user", "text": "Remember the staging rule"},
                          {"role": "assistant", "text": "Noted."}],
             "sid-b": [{"role": "user", "text": "Brief: sync stalls"},
                       {"role": "assistant", "text": "Reopening fixed it."}]}
    monkeypatch.setattr(nightly, "_agent", lambda: _FakeAgent(str(tmp_path), turns))
    monkeypatch.setattr(chat, "state", lambda sid: {"current": "sid-main", "folded": []})
    monkeypatch.setattr(breakouts, "session_ids", lambda sid: {"sid-b": "discarded"})
    calls = stub_side(monkeypatch, fn=_job(MEMORY, []))
    res = nightly.run_job(s.id)
    assert res["status"] == "done" and len(calls) == 1
    sess = os.path.join(s.home, ".specialist", "nightly", res["run"], "input", "sessions")
    b = open(os.path.join(sess, "sid-b.md")).read()
    assert "breakout (discarded)" in b and "AGENT| Reopening fixed it." in b and "<<<TRANSCRIPT" in b
    assert "USER| Remember the staging rule" in open(os.path.join(sess, "sid-main.md")).read()
    assert nightly.state(s.id)["cursors"]["sid-b"]["lines"] == 2
    # unchanged sessions are not read again
    assert nightly.run_job(s.id)["status"] == "nothing" and len(calls) == 1


def test_restart_mid_nightly_closes_interrupted_run(monkeypatch):
    s, said, guess, corr = _setup()

    def die(call):
        with open(os.path.join(call["cwd"], "memory.md"), "a") as fh:
            fh.write("- [m-cccc] half done {basis=guess; seen=1}\n")
        raise KeyboardInterrupt  # the server process dies mid-run

    from fused_render.specialists import harness
    monkeypatch.setattr(harness, "_spawn", die)
    with pytest.raises(KeyboardInterrupt):
        nightly.run_job(s.id)
    st = nightly.state(s.id)
    assert st["active"]["stage"] == "running"
    run = st["active"]["run"]
    rec = nightly.recover(s.id)
    assert rec["status"] == "interrupted" and [it["file"] for it in rec["items"]] == ["memory.md"]
    assert history.log(s.id, limit=1)[0]["message"] == f"nightly {run} (interrupted)"
    assert len(inbox.undigested(s.id)) == 3 and "active" not in nightly.state(s.id)
    assert not nightly.state(s.id).get("last_ok")
    assert nightly.recover(s.id) is None  # nothing twice
    # the next run reads the same notes again
    calls = stub_side(monkeypatch, fn=_job(MEMORY, [], digested=[said["id"]]))
    assert nightly.run_job(s.id)["status"] == "done" and len(calls) == 1


def test_restart_while_applying_finishes_once(monkeypatch):
    s, said, guess, corr = _setup()
    calls = stub_side(monkeypatch, fn=_job(MEMORY + "- [m-dddd] d {basis=guess; seen=1}\n",
                                            [{"kind": "Learned", "text": "d", "file": "memory.md"}],
                                            digested=[said["id"]],
                                            drafts=[{"to": {"name": "Bo"}, "subject": "s", "text": "t"}]))
    real_apply = nightly._apply
    monkeypatch.setattr(nightly, "_apply", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        nightly.run_job(s.id)
    assert nightly.state(s.id)["active"]["stage"] == "applying"
    monkeypatch.setattr(nightly, "_apply", real_apply)
    rec = nightly.recover(s.id)
    assert rec["status"] == "done" and len(calls) == 1
    assert inbox.get(s.id, said["id"])["digested"] and nightly.state(s.id)["last_ok"]
    from fused_render.specialists import drafts
    assert len(drafts.waiting(s.id)) == 1


def test_busy_refuses_and_run_now_route(api, monkeypatch):
    s, *_ = _setup()
    monkeypatch.setattr(nightly, "live", lambda sid: True)
    code, _h, body = api.post(f"/api/specialists/{s.id}/nightly/run", {})
    assert code == 409 and body["reason"] == "busy"
    monkeypatch.setattr(nightly, "live", lambda sid: False)
    calls = stub_side(monkeypatch, fn=_job(MEMORY, []))
    code, _h, body = api.post(f"/api/specialists/{s.id}/nightly/run", {})
    assert code == 202 and body["row"]
    import time
    for _ in range(200):
        if not nightly.running(s.id) and nightly.state(s.id).get("last_ok"):
            break
        time.sleep(0.05)
    code, _h, st = api.get(f"/api/specialists/{s.id}/nightly")
    assert code == 200 and st["running"] is False and st["last_ok"] and len(calls) == 1


def test_validate_caps_and_kinds():
    raw = {"summary": "x" * 900, "items": [{"kind": "Weird", "text": "t" * 900, "entry": "zz"}] * 80,
           "digested": ["n-abc", 3, "bad id"], "repeat_breakouts": -1, "drafts": [{"text": ""}]}
    v = changes.validate(raw)
    assert len(v["items"]) == 50 and v["items"][0]["kind"] == "Changed" and len(v["items"][0]["text"]) == 500
    assert v["items"][0]["entry"] is None and v["digested"] == ["n-abc"] and v["repeat_breakouts"] == 0
    assert v["drafts"] == [] and len(v["summary"]) == 500
    assert changes.validate([]) is None and changes.validate({"items": "x"}) is None
