"""PR status from GitHub (fused_render/pr_status.py) and its endpoint.

Nothing here touches the network: `urlopen` and `gh_token` are replaced, and an
autouse fixture makes any stray socket fail the test.
"""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from fused_render import pr_status
from fused_render.server import create_app


@pytest.fixture(autouse=True)
def no_egress(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("this test tried to open a socket")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    pr_status.reset_for_tests()
    clock = {"t": 1000.0}
    monkeypatch.setattr(pr_status.time, "time", lambda: clock["t"])
    yield clock
    pr_status.reset_for_tests()


class _Resp:
    def __init__(self, data):
        self._b = json.dumps(data).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Gh:
    """A fake GitHub: `states` maps `owner/repo#n` to (state, isDraft) or None."""

    def __init__(self, monkeypatch, states, token="tok"):
        self.calls = []
        self.token_calls = 0
        self.states = states
        self.fail = False
        self.token = token
        monkeypatch.setattr(pr_status, "gh_token", self._token)
        monkeypatch.setattr(pr_status.urllib.request, "urlopen", self._open)

    def _token(self):
        self.token_calls += 1
        return self.token

    def _open(self, req, timeout=None):
        if self.fail:
            raise OSError("boom")
        body = json.loads(req.data)
        self.calls.append((req, body["query"], timeout))
        data = {}
        import re
        for alias, owner, name, num in re.findall(
                r'(r\d+): repository\(owner:"([^"]+)",name:"([^"]+)"\)'
                r'\{ pullRequest\(number:(\d+)\)', body["query"]):
            st = self.states.get(f"{owner}/{name}#{num}")
            data[alias] = None if st is None else {
                "pullRequest": {"state": st[0], "isDraft": st[1],
                                "headRefName": st[2] if len(st) > 2 else None}}
        return _Resp({"data": data})


def test_state_mapping(monkeypatch):
    Gh(monkeypatch, {"a/b#1": ("MERGED", False), "a/b#2": ("CLOSED", False),
                     "a/b#3": ("OPEN", True), "a/b#4": ("OPEN", False),
                     "a/b#5": ("WEIRD", False)})
    got = pr_status.fetch_statuses(
        ["a/b#1", "a/b#2", "a/b#3", "a/b#4", "a/b#5", "a/b#6"])
    assert got == {"a/b#1": "merged", "a/b#2": "closed", "a/b#3": "draft",
                   "a/b#4": "open", "a/b#5": None, "a/b#6": None}


def test_request_shape(monkeypatch):
    gh = Gh(monkeypatch, {"a/b#1": ("OPEN", False)})
    pr_status.fetch_statuses(["a/b#1"])
    req, query, timeout = gh.calls[0]
    assert req.full_url == "https://api.github.com/graphql"
    assert req.get_header("Authorization") == "bearer tok"
    assert timeout == 8
    assert 'repository(owner:"a",name:"b")' in query


def test_open_expires_after_120s_merged_never(monkeypatch, fresh):
    gh = Gh(monkeypatch, {"a/b#1": ("OPEN", False), "a/b#2": ("MERGED", False)})
    pr_status.fetch_statuses(["a/b#1", "a/b#2"])
    assert len(gh.calls) == 1
    fresh["t"] += 119
    pr_status.fetch_statuses(["a/b#1", "a/b#2"])
    assert len(gh.calls) == 1
    fresh["t"] += 2
    pr_status.fetch_statuses(["a/b#1", "a/b#2"])
    assert len(gh.calls) == 2
    assert 'number:1' in gh.calls[1][1] and 'number:2' not in gh.calls[1][1]
    fresh["t"] += 10 ** 7
    pr_status.fetch_statuses(["a/b#2"])
    assert len(gh.calls) == 2


def test_failure_is_none_and_retried_after_60s(monkeypatch, fresh):
    gh = Gh(monkeypatch, {"a/b#1": ("OPEN", False)})
    gh.fail = True
    assert pr_status.fetch_statuses(["a/b#1"]) == {"a/b#1": None}
    gh.fail = False
    fresh["t"] += 59
    assert pr_status.fetch_statuses(["a/b#1"]) == {"a/b#1": None}
    assert gh.calls == []
    fresh["t"] += 2
    assert pr_status.fetch_statuses(["a/b#1"]) == {"a/b#1": "open"}


def test_no_token_means_none_and_no_network(monkeypatch):
    gh = Gh(monkeypatch, {"a/b#1": ("OPEN", False)}, token=None)
    assert pr_status.fetch_statuses(["a/b#1"]) == {"a/b#1": None}
    assert gh.calls == []


def test_bad_refs_are_rejected(monkeypatch):
    gh = Gh(monkeypatch, {})
    bad = ['a/b#1"){x}', "nope", "a/b#", "a/b#12345678", "a b/c#1", "?#3"]
    assert pr_status.fetch_statuses(bad) == {}
    assert gh.calls == []
    assert pr_status.parse_ref("a/b#1") == ("a", "b", 1)
    assert pr_status.parse_ref("a/b#x") is None


def test_thirty_refs_make_two_calls(monkeypatch):
    gh = Gh(monkeypatch, {f"a/b#{n}": ("OPEN", False) for n in range(1, 31)})
    got = pr_status.fetch_statuses([f"a/b#{n}" for n in range(1, 31)])
    assert len(gh.calls) == 2
    assert set(got.values()) == {"open"}


def test_refs_are_deduped_and_capped(monkeypatch):
    gh = Gh(monkeypatch, {})
    pr_status.fetch_statuses(["a/b#1", "a/b#1"])
    assert gh.calls[0][1].count("pullRequest") == 1
    gh.calls.clear()
    pr_status.fetch_statuses([f"a/b#{n}" for n in range(1, 301)])
    assert len(gh.calls) == 4  # 100 refs / 25


def test_token_is_fetched_once_across_calls(monkeypatch):
    gh = Gh(monkeypatch, {"a/b#1": ("OPEN", False), "a/b#2": ("OPEN", False)})
    pr_status.fetch_statuses(["a/b#1"])
    pr_status.fetch_statuses(["a/b#2"])
    assert gh.token_calls == 1


def test_endpoint(monkeypatch, tmp_path):
    seen = {}

    def fake(refs):
        seen["refs"] = refs
        return {"a/b#1": "open"}

    monkeypatch.setattr(pr_status, "fetch_statuses", fake)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.post("/api/tasks/pr-status", json={"refs": ["a/b#1"]},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"a/b#1": "open"}
    assert seen["refs"] == ["a/b#1"]
    assert client.post("/api/tasks/pr-status", json={"refs": []}).status_code in (
        400, 403)


def test_head_branch_is_fetched_and_the_state_shape_is_unchanged(monkeypatch):
    gh = Gh(monkeypatch, {"a/b#1": ("OPEN", False, "feat/x"),
                          "a/b#2": ("MERGED", False)})
    got = pr_status.fetch_statuses(["a/b#1", "a/b#2"])
    assert got == {"a/b#1": "open", "a/b#2": "merged"}
    assert "headRefName" in gh.calls[0][1]
    assert pr_status.head_refs(["a/b#1", "a/b#2"]) == {"a/b#1": "feat/x"}


def test_endpoint_adds_heads_only_when_known(monkeypatch, tmp_path):
    Gh(monkeypatch, {"a/b#1": ("OPEN", False, "feat/x"), "a/b#2": ("OPEN", False)})
    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.post("/api/tasks/pr-status", json={"refs": ["a/b#1", "a/b#2"]},
                    headers={"X-Fused": "1"})
    assert r.json() == {"a/b#1": "open", "a/b#2": "open",
                        "_heads": {"a/b#1": "feat/x"}}
