"""The UDF worker process itself (`fused_render/udf/_worker.py`, SPEC §54.4),
run for real with a stub `fused` package first on its path: the protocol,
the batch/map gate (SS-5), the realtime vs batch call, what reaches stdout."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

STUB = textwrap.dedent('''
    import json, os, types

    options = types.SimpleNamespace(base_url="https://stub")
    LOG = os.environ["STUB_LOG"]

    def _log(kind, **kw):
        with open(LOG, "a") as fh:
            fh.write(json.dumps(dict(kw, call=kind), default=str) + "\\n")

    def _env(name):
        _log("env", name=name)

    class Udf:
        def __init__(self, code, name):
            self.code, self.name, self.collection_name = code, name, None

    def load(source, collection_name=None, **kw):
        if "missing" in source:
            raise ValueError("UDF not found")
        if source.endswith(".py"):
            with open(source) as fh:
                return Udf(fh.read(), os.path.basename(source)[:-3])
        return Udf("@fused.udf\\ndef udf():\\n    return 1\\n", source)

    class Result:
        def __init__(self, content=b"", headers=None, data=None):
            self.original_content, self.original_headers, self.data = content, headers or {}, data
            self.stdout, self.stderr, self.is_cached, self.time_taken_seconds = "out", "", True, 0.1
            self.error_message, self.has_exception, self.error_lineno = None, False, None

    async def run_async(udf, **kw):
        print("SDK noise on stdout")
        _log("run_async", fmt=kw.get("_format"), params=kw.get("parameters"), cache=kw.get("cache"),
             collection=udf.collection_name)
        if kw.get("_format"):
            return Result(b"<p>ok</p>", {"content-type": "text/html"})
        return Result(b"PAR1xxxx", {}, data=[{"a": 1}])

    def run(udf, **kw):
        print("Batch job started")
        _log("run", fmt=kw.get("_format"))
        return Result(b"", {}, data="batch value")
''')


@pytest.fixture
def worker(tmp_path):
    stub_dir = tmp_path / "stub"
    (stub_dir / "fused").mkdir(parents=True)
    (stub_dir / "fused" / "__init__.py").write_text(STUB, encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run(requests):
        env = dict(os.environ, STUB_LOG=str(log), FUSED_ENV="prod",
                   PYTHONPATH=os.pathsep.join([str(stub_dir), repo]))
        proc = subprocess.run([sys.executable, "-m", "fused_render.udf._worker"],
                              input="".join(json.dumps(r) + "\n" for r in requests),
                              capture_output=True, text=True, timeout=60, env=env)
        events = [json.loads(line) for line in proc.stdout.splitlines()]
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return events, calls, proc

    run.tmp = tmp_path
    return run


def _udf(tmp_path, name, code):
    path = tmp_path / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    return str(path)


def test_a_plain_udf_runs_async_and_only_answers_reach_stdout(worker):
    src = _udf(worker.tmp, "plain", "@fused.udf\ndef udf(n: int = 1):\n    return n\n")
    out = str(worker.tmp / "a.body")
    events, calls, proc = worker([{"op": "run", "id": "a", "source": src, "collection": "demo",
                                   "params": {"n": 2}, "format": "auto", "out": out}])
    assert [e["event"] for e in events] == ["started", "done"]
    assert events[0]["kind"] == "none"
    done = events[1]
    assert done["ok"] and done["content_type"] == "text/html" and done["size"] == 9
    assert done["fused_cached"] is True and done["stdout"] == "out"
    assert open(out, "rb").read() == b"<p>ok</p>"
    assert "SDK noise" not in proc.stdout and "SDK noise" in proc.stderr
    run_call = [c for c in calls if c["call"] == "run_async"][0]
    assert run_call == {"call": "run_async", "fmt": "png,html", "params": {"n": 2},
                        "cache": True, "collection": "demo"}
    assert {"call": "env", "name": "prod"} in calls


def test_a_map_udf_is_not_run_without_confirmation(worker):
    src = _udf(worker.tmp, "mapper",
               "@fused.udf\ndef udf():\n    inner = fused.load('x')\n    return inner.map([1, 2])\n")
    events, calls, _ = worker([{"op": "run", "id": "m", "source": src, "out": str(worker.tmp / "m.body")}])
    assert events == [{"id": "m", "event": "needs_confirmation", "kind": "map"}]
    assert not [c for c in calls if c["call"] in ("run", "run_async")]


def test_a_confirmed_batch_job_runs_through_the_sync_call(worker):
    src = _udf(worker.tmp, "job", "@fused.udf(engine='small')\ndef udf():\n    return 1\n")
    out = str(worker.tmp / "b.body")
    events, calls, proc = worker([{"op": "run", "id": "b", "source": src, "confirmed": True,
                                   "out": out}])
    assert events[0] == {"id": "b", "event": "started", "kind": "decorator-batch",
                         "code_sha256": events[0]["code_sha256"]}
    assert events[1]["ok"] and open(out, "rb").read() == b"batch value"
    assert [c["call"] for c in calls if c["call"] in ("run", "run_async")] == ["run"]
    assert "Batch job started" not in proc.stdout


def test_json_and_parquet_formats(worker):
    src = _udf(worker.tmp, "t", "@fused.udf\ndef udf():\n    return 1\n")
    events, calls, _ = worker([
        {"op": "run", "id": "j", "source": src, "format": "json", "out": str(worker.tmp / "j.body")},
        {"op": "run", "id": "p", "source": src, "format": "parquet", "cache": False,
         "out": str(worker.tmp / "p.body")},
    ])
    done = {e["id"]: e for e in events if e["event"] == "done"}
    assert done["j"]["content_type"] == "application/json"
    assert json.loads((worker.tmp / "j.body").read_text()) == [{"a": 1}]
    assert done["p"]["content_type"] == "application/vnd.apache.parquet"
    fmts = sorted((c["fmt"] is None, c["cache"]) for c in calls if c["call"] == "run_async")
    assert fmts == [(True, False), (True, True)]


def test_a_udf_that_cannot_load_is_an_answer(worker):
    events, _, proc = worker([{"op": "run", "id": "x", "source": "missing", "collection": "c",
                               "out": str(worker.tmp / "x.body")}])
    assert events == [{"id": "x", "event": "done", "ok": False,
                       "error": "could not load this as a UDF: UDF not found",
                       "line": None, "stdout": "", "stderr": ""}]
    assert proc.returncode == 0
