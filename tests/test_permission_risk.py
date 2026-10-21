import json
import threading

import pytest

from fused_render import permission_risk as pr


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_PERMISSION_RISK", raising=False)
    pr.clear_cache()
    yield
    pr.clear_cache()


def _call(reply, seen=None):
    def call(prompt, system_prompt, timeout):
        if seen is not None:
            seen.append((prompt, system_prompt, timeout))
        if isinstance(reply, Exception):
            raise reply
        return reply
    return call


def test_plain_json():
    out = pr.assess("Bash", {"command": "ls"}, call=_call('{"severity":"low","summary":"Lists files."}'))
    assert out == {"severity": "low", "summary": "Lists files."}


def test_fence_prose_and_case():
    raw = 'Sure!\n```json\n{"severity": "HIGH ", "summary": "  Deletes it.  "}\n```\nbye'
    assert pr.assess("Bash", {"c": 1}, call=_call(raw)) == {"severity": "high", "summary": "Deletes it."}


def test_summary_capped():
    raw = json.dumps({"severity": "medium", "summary": "x" * 500})
    assert len(pr.assess("T", {}, call=_call(raw))["summary"]) == 200


@pytest.mark.parametrize("raw", ["", "nope", '{"severity":"scary","summary":"a"}',
                                 '{"severity":"low"}', '{"severity":"low","summary":""}', None, 5])
def test_invalid_is_none(raw):
    assert pr.assess("T", {}, call=_call(raw)) is None


def test_exception_is_none():
    assert pr.assess("T", {}, call=_call(TimeoutError("slow"))) is None


def test_cache_successes_only():
    seen = []
    pr.assess("T", {"a": 1}, call=_call("bad", seen))
    pr.assess("T", {"a": 1}, call=_call("bad", seen))
    assert len(seen) == 2
    ok = '{"severity":"low","summary":"ok"}'
    pr.assess("T", {"a": 1}, call=_call(ok, seen))
    pr.assess("T", {"a": 1}, call=_call(ok, seen))
    assert len(seen) == 3


def test_cache_bounded():
    ok = '{"severity":"low","summary":"ok"}'
    for i in range(300):
        pr.assess("T", {"i": i}, call=_call(ok))
    assert len(pr._cache) == 256


def test_prompt_marks_untrusted_and_truncates():
    seen = []
    pr.assess("Bash", {"command": "y" * 9000}, call=_call('{"severity":"low","summary":"s"}', seen), timeout=3)
    prompt, system, timeout = seen[0]
    assert "UNTRUSTED" in prompt and "truncated" in prompt and len(prompt) < 5000
    assert "low" in system and "high" in system and timeout == 3


def test_thread_safe_cache():
    ok = '{"severity":"low","summary":"ok"}'
    ts = [threading.Thread(target=lambda i=i: pr.assess("T", {"i": i % 5}, call=_call(ok))) for i in range(40)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(pr._cache) == 5


def test_off_switch(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_PERMISSION_RISK", "0")
    assert pr.assess("T", {}, call=_call('{"severity":"low","summary":"ok"}')) is None
