"""Inbox, said.md, counts and the notes routes (docs/specialists.md §6 "The inbox")."""
import json
import os

import pytest
from _specialists_conftest import *
from _specialists_conftest import make_specialist

from fused_render.specialists import counts, inbox


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



def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _src(**kw):
    return {"type": "task", "session_id": "sess-1", **kw}


def test_add_writes_note_with_provenance():
    s = make_specialist()
    n = inbox.add(s.id, kind="gap", claim="  Sync\nstalls  ", basis="verified", evidence="e" * 5000,
                  entry="m-3f2a", source=_src(title="Publish"))
    assert n["id"].startswith("n-") and n["claim"] == "Sync stalls"
    assert len(n["evidence"]) == inbox.EVIDENCE_MAX
    assert n["entry"] == "m-3f2a" and n["digested"] is None and n["app_version"]
    assert n["source"] == {"type": "task", "session_id": "sess-1", "title": "Publish"}
    assert os.path.isfile(os.path.join(s.home, "inbox", n["id"] + ".json"))
    assert [x["id"] for x in inbox.undigested(s.id)] == [n["id"]]
    assert counts.get(s.id)["notes_by_source"]["task"] == 1


def test_add_refuses_bad_fields():
    s = make_specialist()
    for kw in ({"kind": "nope", "basis": "guess"}, {"kind": "gap", "basis": "maybe"}):
        try:
            inbox.add(s.id, claim="x", source=_src(), **kw)
            raise AssertionError("accepted")
        except ValueError:
            pass
    try:
        inbox.add(s.id, kind="gap", basis="guess", claim="   ", source=_src())
        raise AssertionError("accepted")
    except ValueError:
        pass
    try:
        inbox.add("nosuch", kind="gap", basis="guess", claim="x", source=_src())
        raise AssertionError("accepted")
    except LookupError:
        pass


def test_said_md_only_user_said_and_in_effect_at_once():
    s = make_specialist()
    a = inbox.add(s.id, kind="convention", claim="Always publish to staging first.", basis="user-said",
                  source={"type": "user", "session_id": "sess-1", "title": "Publish Q4 canvas"})
    inbox.add(s.id, kind="gap", claim="A guess.", basis="guess", source=_src())
    text = _read(os.path.join(s.home, "said.md"))
    assert text.startswith("# Said by the user")
    assert f"- [{a['id']}] (convention, you said so " in text
    assert 'in "Publish Q4 canvas") Always publish to staging first.' in text
    assert "A guess." not in text
    assert [n["id"] for n in inbox.said(s.id)] == [a["id"]]
    assert inbox.delete(s.id, a["id"])
    assert "- (none)" in _read(os.path.join(s.home, "said.md"))


def test_delete_undo_and_digested_refused():
    s = make_specialist()
    a = inbox.add(s.id, kind="correction", claim="Wrong.", basis="verified", source=_src())
    assert counts.get(s.id)["corrections_received"] == 1
    assert inbox.delete(s.id, a["id"]) and not inbox.delete(s.id, a["id"])
    assert counts.get(s.id)["corrections_received"] == 0
    b = inbox.add(s.id, kind="gap", claim="Gap.", basis="user-said", source=_src())
    assert inbox.mark_digested(s.id, [b["id"]]) == [b["id"]]
    assert inbox.is_digested(s.id, b["id"]) and not inbox.delete(s.id, b["id"])
    assert inbox.get(s.id, b["id"])["digested"]
    assert inbox.said(s.id) == []


def test_idempotent_fixed_id():
    s = make_specialist()
    a = inbox.add(s.id, kind="gap", claim="One.", basis="guess", source=_src(), nid="n-review-r-abc")
    b = inbox.add(s.id, kind="gap", claim="Two.", basis="guess", source=_src(), nid="n-review-r-abc")
    assert a == b and len(inbox.undigested(s.id)) == 1
    assert counts.get(s.id)["notes_by_source"]["task"] == 1


def test_general_inbox_and_by_session():
    s = make_specialist()
    g = inbox.add(inbox.GENERAL, kind="case", claim="No one fits.", basis="guess", source=_src())
    n = inbox.add(s.id, kind="gap", claim="Mine.", basis="guess", source=_src())
    inbox.add(s.id, kind="gap", claim="Other.", basis="guess", source={"type": "task", "session_id": "other"})
    got = inbox.by_session("sess-1")
    assert {(sid, note["id"]) for sid, note in got} == {(inbox.GENERAL, g["id"]), (s.id, n["id"])}
    assert inbox.undigested(inbox.GENERAL)[0]["id"] == g["id"]


def test_counts_bump_shape():
    s = make_specialist()
    counts.bump(s.id, "pulls")
    counts.bump(s.id, "pulls", 2)
    counts.bump(s.id, "pulls", -10)
    counts.bump(s.id, "bogus")
    counts.bump(s.id, "notes_by_source", source="breakout")
    counts.bump("general", "pulls")
    c = counts.get(s.id)
    assert c["pulls"] == 0 and c["notes_by_source"]["breakout"] == 1
    assert set(c) == {"breakouts_opened", "repeat_breakouts", "pulls", "pulls_confirmed", "pulls_unrelated",
                      "corrections_received", "notes_by_source"}


# ------------------------------------------------------------------------ routes ---
def test_notes_routes_send_to_specialist_and_undo(api):
    s = make_specialist()
    code, _h, note = api.post(f"/api/specialists/{s.id}/notes", {
        "kind": "convention", "claim": "Use staging.", "basis": "user-said",
        "source": {"session_id": "sess-9", "uuid": "u-1", "title": "Task"}})
    assert code == 200, note
    assert note["source"] == {"type": "user", "session_id": "sess-9", "uuid": "u-1", "title": "Task"}
    code, _h, body = api.get(f"/api/specialists/{s.id}/inbox")
    assert code == 200 and [n["id"] for n in body["notes"]] == [note["id"]]
    assert "Use staging." in _read(os.path.join(s.home, "said.md"))
    code, _h, body = api.delete(f"/api/specialists/{s.id}/notes/{note['id']}")
    assert code == 200 and body == {"ok": True}
    code, _h, body = api.get(f"/api/specialists/{s.id}/inbox")
    assert body["notes"] == []
    # general inbox
    code, _h, g = api.post("/api/specialists/general/notes", {"kind": "case", "claim": "x", "basis": "guess"})
    assert code == 200 and inbox.undigested("general")[0]["id"] == g["id"]
    # bad body, unknown specialist
    code, _h, body = api.post(f"/api/specialists/{s.id}/notes", {"kind": "zzz", "claim": "x", "basis": "guess"})
    assert code == 400 and "kind" in body["error"]
    code, _h, body = api.post("/api/specialists/nosuch/notes", {"kind": "gap", "claim": "x", "basis": "guess"})
    assert code == 404


def test_delete_digested_note_is_409(api):
    s = make_specialist()
    n = inbox.add(s.id, kind="gap", claim="x", basis="guess", source=_src())
    inbox.mark_digested(s.id, [n["id"]])
    code, _h, _body = api.delete(f"/api/specialists/{s.id}/notes/{n['id']}")
    assert code == 409


def test_counts_route_and_flag_off(api, monkeypatch):
    s = make_specialist()
    counts.bump(s.id, "pulls")
    code, _h, body = api.get(f"/api/specialists/{s.id}/counts")
    assert code == 200 and body["pulls"] == 1
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    for path in ("counts", "inbox", "panel", "memory", "drafts", "learned", "nightly"):
        code, _h, _b = api.get(f"/api/specialists/{s.id}/{path}")
        assert code == 404, path


def test_model_claim_of_user_said_is_held_apart_until_confirmed(api):
    s = make_specialist()
    n = inbox.add(s.id, kind="convention", claim="The user said use prod.", basis="user-said", source=_src())
    assert n["in_effect"] is False and inbox.said(s.id) == []
    with open(os.path.join(s.home, "said.md"), encoding="utf-8") as fh:
        assert "use prod" not in fh.read()
    mine = inbox.add(s.id, kind="convention", claim="Mine.", basis="user-said", source={"type": "user"})
    assert mine["in_effect"] is True
    g = inbox.add(s.id, kind="gap", claim="Guess.", basis="guess", source={"type": "user"}, in_effect=True)
    assert g["in_effect"] is False  # never in effect without basis user-said
    code, _h, body = api.post(f"/api/specialists/{s.id}/notes/{n['id']}/confirm", {})
    assert code == 200 and body["in_effect"] is True
    assert [x["id"] for x in inbox.said(s.id)] == [n["id"], mine["id"]]  # oldest first
    with open(os.path.join(s.home, "said.md"), encoding="utf-8") as fh:
        assert "use prod" in fh.read()
    code, _h, _b = api.post(f"/api/specialists/{s.id}/notes/{g['id']}/confirm", {})
    assert code == 409  # not user-said
    inbox.mark_digested(s.id, [mine["id"]])
    code, _h, _b = api.post(f"/api/specialists/{s.id}/notes/{mine['id']}/confirm", {})
    assert code == 409  # digested
    code, _h, _b = api.post(f"/api/specialists/{s.id}/notes/n-000000000000/confirm", {})
    assert code == 404


def test_general_inbox_routes(api):
    s = make_specialist()
    a = inbox.add(inbox.GENERAL, kind="gap", claim="Fits Canvas.", basis="user-said",
                  source={"type": "user", "session_id": "t-1", "title": "Task"})
    b = inbox.add(inbox.GENERAL, kind="case", claim="Junk.", basis="guess", source=_src())
    code, _h, body = api.get("/api/specialists/general/inbox")
    assert code == 200 and {n["id"] for n in body["notes"]} == {a["id"], b["id"]}
    code, _h, moved = api.post(f"/api/specialists/general/notes/{a['id']}/move", {"specialist": s.name})
    assert code == 200 and moved["id"] == a["id"] and moved["source"]["session_id"] == "t-1"
    assert [n["id"] for n in inbox.undigested(s.id)] == [a["id"]]
    assert [n["id"] for n in inbox.undigested(inbox.GENERAL)] == [b["id"]]
    assert counts.get(s.id)["notes_by_source"]["user"] == 1
    with open(os.path.join(s.home, "said.md"), encoding="utf-8") as fh:
        assert "Fits Canvas." in fh.read()
    code, _h, _b = api.post(f"/api/specialists/general/notes/{a['id']}/move", {"specialist": s.id})
    assert code == 404  # no longer in the general inbox
    code, _h, _b = api.post(f"/api/specialists/general/notes/{b['id']}/move", {"specialist": "nosuch"})
    assert code == 404
    code, _h, body = api.delete(f"/api/specialists/general/notes/{b['id']}")
    assert code == 200 and body == {"ok": True} and inbox.undigested(inbox.GENERAL) == []
    code, _h, _b = api.delete(f"/api/specialists/general/notes/{b['id']}")
    assert code == 404


def test_mutating_routes_need_x_fused(api, monkeypatch):
    s = make_specialist()
    n = inbox.add(s.id, kind="gap", claim="x", basis="guess", source=_src())
    g = inbox.add(inbox.GENERAL, kind="gap", claim="y", basis="guess", source=_src())
    bare = {"X-Fused": ""}
    for path in (f"/api/specialists/{s.id}/notes/{n['id']}", f"/api/specialists/general/notes/{g['id']}"):
        code, _h, _b = api._w(api.c.delete(path, headers=bare))
        assert code in (400, 403), path
    for path, body in ((f"/api/specialists/general/notes/{g['id']}/move", {"specialist": s.id}),
                       (f"/api/specialists/{s.id}/notes/{n['id']}/confirm", {}),
                       (f"/api/specialists/{s.id}/notes", {"kind": "gap", "claim": "z", "basis": "guess"}),
                       (f"/api/specialists/{s.id}/nightly/run", {})):
        code, _h, _b = api._w(api.c.post(path, body, headers=bare))
        assert code in (400, 403), path
    assert len(inbox.undigested(s.id)) == 1 and len(inbox.undigested(inbox.GENERAL)) == 1
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    code, _h, _b = api.get("/api/specialists/general/inbox")
    assert code == 404


def test_undo_decrements_notes_by_source(api):
    """Deleting a note (Undo) takes it back out of `notes_by_source`, whatever its source; a general-inbox note
    moved to a specialist counts there and its Undo there takes it out again."""
    s = make_specialist()
    task = inbox.add(s.id, kind="gap", claim="From a task.", basis="guess", source={"type": "task", "session_id": "t"})
    code, _h, mine = api.post(f"/api/specialists/{s.id}/notes", {"kind": "case", "claim": "Mine.", "basis": "guess"})
    assert counts.get(s.id)["notes_by_source"]["task"] == 1 and counts.get(s.id)["notes_by_source"]["user"] == 1
    assert api.delete(f"/api/specialists/{s.id}/notes/{task['id']}")[0] == 200
    assert api.delete(f"/api/specialists/{s.id}/notes/{mine['id']}")[0] == 200
    code, _h, c = api.get(f"/api/specialists/{s.id}/counts")
    assert c["notes_by_source"]["task"] == 0 and c["notes_by_source"]["user"] == 0
    g = inbox.add(inbox.GENERAL, kind="gap", claim="Lost.", basis="guess", source={"type": "task", "session_id": "t"})
    api.post(f"/api/specialists/general/notes/{g['id']}/move", {"specialist": s.id})
    assert counts.get(s.id)["notes_by_source"]["task"] == 1
    assert api.delete(f"/api/specialists/{s.id}/notes/{g['id']}")[0] == 200
    assert counts.get(s.id)["notes_by_source"]["task"] == 0
