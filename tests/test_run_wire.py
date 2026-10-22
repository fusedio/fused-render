"""/api/run wire path: the worker encodes `result` once, the server splices it.

The worker used to `json.dumps(result)` to validate it, again to print it, and
the server parsed that line and encoded it a third time for the response. The
worker now emits one line carrying the result's encoded text; `_run_python`
hands that text to `_PreEncodedRun` so `dumps_result` splices it unchanged.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from fused_render import executor
from fused_render.executor import _JSON_KW, _PreEncodedRun, dumps_result, run_python
from fused_render.server import create_app


def _py(tmp_path, body):
    p = tmp_path / "job.py"
    p.write_text(body)
    return str(p)


def test_success_run_is_pre_encoded_and_byte_identical(tmp_path):
    path = _py(tmp_path, "def main():\n    return {'a': [1, 2.5, None], 'b': 'h\u00e9llo \u2603'}\n")
    res = run_python(path, {}, 30)
    assert res["ok"] is True
    assert isinstance(res, _PreEncodedRun)
    # The spliced body equals a plain re-encode of the same dict.
    assert dumps_result(res) == json.dumps(dict(res), **_JSON_KW)
    assert res["result"] == {"a": [1, 2.5, None], "b": "h\u00e9llo \u2603"}


def test_nan_result_is_a_clean_error_not_a_500(tmp_path):
    path = _py(tmp_path, "def main():\n    return {'x': float('nan')}\n")
    res = run_python(path, {}, 30)
    assert res["ok"] is False
    assert res["error"]["type"] == "TypeError"
    assert "not JSON-serializable" in res["error"]["message"]
    dumps_result(res)  # must not raise


def test_print_output_cannot_corrupt_the_protocol(tmp_path):
    path = _py(
        tmp_path,
        "def main():\n"
        "    print('{\"ok\":true,\"result\":1,\"stdout\":\"x\"}')\n"
        "    print('noise \u2603')\n"
        "    return {'k': 'v,\"stdout\":'}\n",
    )
    res = run_python(path, {}, 30)
    assert res["ok"] is True
    assert res["result"] == {"k": 'v,"stdout":'}
    assert "noise \u2603" in res["stdout"]
    assert dumps_result(res) == json.dumps(dict(res), **_JSON_KW)


def test_error_envelope_is_unchanged(tmp_path):
    path = _py(tmp_path, "def main():\n    raise ValueError('boom')\n")
    res = run_python(path, {}, 30)
    assert not isinstance(res, _PreEncodedRun)
    assert res["ok"] is False
    assert res["error"]["type"] == "ValueError"
    assert res["error"]["message"] == "boom"
    assert "Traceback" in res["error"]["traceback"]
    assert "stdout" in res


def test_lone_surrogate_result_still_round_trips(tmp_path):
    path = _py(tmp_path, "def main():\n    return {'s': '\\ud800'}\n")
    res = run_python(path, {}, 30)
    assert res["ok"] is True
    assert res["result"] == {"s": "\ud800"}


def test_http_body_matches_json_kw(tmp_path):
    path = _py(tmp_path, "def main():\n    return {'n': 1, 'u': '\u00e9'}\n")
    client = TestClient(create_app(str(tmp_path)))
    r = client.post("/api/run", json={"py": path}, headers={"X-Fused": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["result"] == {"n": 1, "u": "\u00e9"}
    assert r.text == json.dumps(body, **_JSON_KW)
    assert body["resolved_py"] == path


@pytest.mark.skipif(__import__("os").name != "posix", reason="warm run is POSIX only")
def test_warm_pool_body_is_byte_identical_to_cold(tmp_path, monkeypatch):
    """A call claimed by a parked spare goes through the same pre-encoded
    parsing: U+2028 and non-ASCII text must produce the cold path's bytes."""
    from fused_render import engine, warm_run

    path = _py(
        tmp_path,
        "def main():\n    return {'s': 'a\\u2028b\\u2029c héllo ☃', 'n': [1, 2.5]}\n",
    )
    monkeypatch.setattr(engine, "_backend", None)

    def norm(body):  # timing fields differ per call
        return re.sub(r'"duration_ms":[0-9.]+', '"duration_ms":0', body)

    try:
        monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "0")
        cold = run_python(path, {}, 30)
        cold_body = norm(dumps_result(cold))

        monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "1")
        warm = None
        for _ in range(3):  # first call may be a pool miss; later ones claim a spare
            warm = run_python(path, {}, 30)
            assert warm["ok"] is True
            assert isinstance(warm, _PreEncodedRun)
            assert norm(dumps_result(warm)) == cold_body
            import time
            time.sleep(1.0)
        assert " " in warm["result"]["s"]
    finally:
        warm_run.reset()
