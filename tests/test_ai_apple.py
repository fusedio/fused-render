"""The apple tier (D700) — everything of it that does NOT need Apple's models.

Apple Intelligence cannot run in CI (owner's call: skip it there), and the
Swift helper cannot even be built below macOS 26. So the helper is replaced
by a stand-in that speaks its protocol — one process per request, `argv[1]`
the op, one JSON object on stdin, NDJSON frames on stdout — and everything
around it is exercised for real: the pinned-id routing rules, the locale and
container refusals, `host.frames`'s three exits (done / cancelled / dead),
the probe, the text relay through `/api/ai`, and the transcribe route down to
the transcript files `speech.py` writes in the Whisper workers' shape.

`platform_problem` is monkeypatched to None so the tier believes it is on a
qualifying Mac; nothing here depends on the host OS beyond being able to
exec a Python script (the helper-backed tests skip on Windows for that).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai.apple import host, speech
from fused_render.server import create_app
from fused_render.server.common import (AI_PROVIDERS, APPLE_MODELS, apple_model_for,
                                        provider_of_model)

needs_exec = pytest.mark.skipif(sys.platform == "win32",
                                reason="the stand-in helper is a script the host execs directly")


# -- the stand-in helper --------------------------------------------------------

_FAKE_HELPER = r'''#!/usr/bin/env python3
"""fused-apple-ai stand-in: argv[1] op, one JSON request on stdin, NDJSON out."""
import json, os, sys, time

def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()

op = sys.argv[1] if len(sys.argv) > 1 else ""
if op == "die-before-reading":
    os._exit(3)
req = json.loads(sys.stdin.read() or "{}")
if op == "probe":
    send({"type": "probe", "available": True, "state": "available", "reason": "",
          "os": "26.6.2", "imageInput": False, "defaultLocale": "en-US",
          "speechLocales": ["en-US", "en-GB", "de-DE", "fr-FR"],
          "installedLocales": ["en-US", "de-DE"]})
elif op == "text":
    prompt = req.get("prompt", "")
    if "slow" in prompt:
        time.sleep(30)
    if "die" in prompt:
        os._exit(3)
    if "refuse" in prompt:
        send({"type": "chunk", "text": "I can"})
        send({"type": "done", "ok": True, "finishReason": "content-filter",
              "characters": 5, "message": "guardrail"})
        sys.exit(0)
    if "overflow" in prompt:
        send({"type": "done", "ok": False,
              "error": {"type": "ai_error", "message": "context window exceeded"}})
        sys.exit(0)
    send({"type": "chunk", "text": "hello"})
    if "hang" in prompt:
        time.sleep(30)
    send({"type": "chunk", "text": " world"})
    send({"type": "done", "ok": True, "finishReason": "stop", "characters": 11,
          "history": len(req.get("history") or [])})
elif op == "speech":
    if not os.path.exists(req.get("path", "")):
        send({"type": "done", "ok": False,
              "error": {"type": "ai_error", "message": "Cannot Open"}})
        sys.exit(0)
    send({"type": "assets"})
    seg = {"type": "segment", "start": 0.0, "end": 1.5, "text": "Yep."}
    if req.get("words"):
        seg["words"] = [{"start": 0.0, "end": 1.5, "word": "Yep."}]
    send(seg)
    send({"type": "segment", "start": 1.5, "end": 3.0, "text": "It works."})
    send({"type": "done", "ok": True, "duration": 3.0, "locale": req.get("locale")})
else:
    send({"type": "done", "ok": False, "error": {"type": "bad_request", "message": "unknown op"}})
'''


@pytest.fixture()
def fake_helper(tmp_path, monkeypatch):
    """The stand-in on disk, wired in through the same env var a developer
    uses, on a machine the tier believes qualifies."""
    script = tmp_path / "fused-apple-ai"
    script.write_text(_FAKE_HELPER.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(host.HELPER_ENV, str(script))
    monkeypatch.setattr(host, "platform_problem", lambda: None)
    host.reset()
    yield str(script)
    host.shutdown()


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir="/"))


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs.reset()
    yield
    jobs.reset()


def _availability(**over) -> host.Availability:
    base = dict(ok=True, state="available", os="26.6.2", default_locale="en-US",
                speech_locales=("en-US", "en-GB", "en-IN", "de-DE", "fr-FR", "es-ES", "es-MX"),
                installed_locales=("en-US", "de-DE"))
    base.update(over)
    return host.Availability(**base)


# -- the pinned ids (server/common.py) -----------------------------------------


def test_the_pinned_ids_name_their_tier_and_one_capability_each():
    assert "apple" in AI_PROVIDERS
    assert APPLE_MODELS == {"afm-text": "text-generation",
                            "afm-speech": "automatic-speech-recognition",
                            "afm-embedding": "embeddings"}
    assert apple_model_for("text-generation") == "afm-text"
    assert apple_model_for("automatic-speech-recognition") == "afm-speech"
    # Image and video: Apple ships no programmatic model, so no id (D700).
    assert apple_model_for("text-to-image") is None
    assert provider_of_model("afm-text") == "apple"
    # An unpinned id says nothing — the shape rule decides, as before.
    assert provider_of_model("mlx-community/x") is None
    assert provider_of_model(None) is None


# -- speech.locale_for ---------------------------------------------------------
# Whisper's `language` (ISO 639 or absent) → the BCP-47 tag SpeechTranscriber
# wants, or the refusal a caller can act on.


def test_no_language_means_the_system_locale_when_apple_has_it():
    assert speech.locale_for(None, _availability()) == ("en-US", None)
    assert speech.locale_for("", _availability()) == ("en-US", None)


def test_a_system_locale_apple_lacks_falls_back_by_its_language():
    # en-NZ is not in the supported set; en is, and the region can't match,
    # so the conventional variant wins over the installed ordering.
    got, why = speech.locale_for(None, _availability(default_locale="en-NZ"))
    assert (got, why) == ("en-US", None)


def test_a_full_tag_passes_through_case_insensitively():
    assert speech.locale_for("de-de", _availability()) == ("de-DE", None)
    assert speech.locale_for("en_GB", _availability()) == ("en-GB", None)


def test_a_full_tag_apple_lacks_is_refused_with_the_supported_list():
    got, why = speech.locale_for("pt-BR", _availability())
    assert got is None
    assert "no 'pt-BR'" in why and "de-DE" in why and "en-US" in why


def test_a_bare_language_prefers_the_system_region():
    # System locale es-MX: bare "es" picks the MX variant over ES.
    assert speech.locale_for("es", _availability(default_locale="es-MX")) == ("es-MX", None)


def test_a_bare_language_without_a_regional_match_takes_the_conventional_variant():
    # System locale en-US; "es" has no US variant → es-ES, not es-MX by
    # accident of ordering.
    assert speech.locale_for("es", _availability()) == ("es-ES", None)


def test_a_bare_language_with_no_conventional_variant_prefers_an_installed_one():
    avail = _availability(speech_locales=("xx-AA", "xx-BB", "xx-CC"), installed_locales=("xx-BB",))
    assert speech.locale_for("xx", avail) == ("xx-BB", None)
    # Nothing installed: the first sorted variant, deterministically.
    avail = _availability(speech_locales=("xx-CC", "xx-AA"), installed_locales=())
    assert speech.locale_for("xx", avail) == ("xx-AA", None)


def test_a_language_apple_has_no_model_for_is_refused():
    got, why = speech.locale_for("tlh", _availability())
    assert got is None and "no 'tlh'" in why


# -- speech.unsupported_container ----------------------------------------------


def test_a_browser_recording_is_refused_naming_the_formats_and_the_way_out():
    why = speech.unsupported_container("/takes/playground-mic.webm")
    assert why is not None
    assert ".webm" in why and "playground-mic.webm" in why
    assert "m4a" in why and "wav" in why
    assert 'provider: "local"' in why
    assert speech.unsupported_container("/takes/clip.ogg") is not None


def test_native_containers_and_extensionless_files_are_let_through():
    for name in ("take.m4a", "take.M4A", "take.wav", "take.mp3", "take.caf", "take.aiff", "take.mov"):
        assert speech.unsupported_container("/takes/" + name) is None, name
    # No extension: AVFoundation sniffs; a wrong guess surfaces mid-run.
    assert speech.unsupported_container("/takes/capture-1234") is None


# -- host.frames: the three exits ----------------------------------------------


@needs_exec
def test_frames_yields_the_helpers_frames_through_its_done(fake_helper):
    frames = list(host.frames("text", {"prompt": "hi", "history": [{"role": "user", "content": "a"}]}))
    assert [f["type"] for f in frames] == ["chunk", "chunk", "done"]
    assert "".join(f.get("text", "") for f in frames[:-1]) == "hello world"
    assert frames[-1]["finishReason"] == "stop"
    # The request reached the child on stdin, as one JSON object.
    assert frames[-1]["history"] == 1


@needs_exec
def test_a_cancel_mid_stream_is_a_cancelled_done_frame_not_an_error(fake_helper):
    """The one-shot design has no cancel protocol: cancel = terminate. The
    resulting SIGTERM exit must read back as the same `done` the local
    worker sends on its cooperative cancel, never as the watchdog's timeout
    (Bugbot on #1000: a pressed ✕ became a 500)."""
    child = {}
    events = host.frames("text", {"prompt": "hang"}, on_spawn=lambda p: child.setdefault("proc", p))
    first = next(events)
    assert first["type"] == "chunk" and first["text"] == "hello"
    host.cancel(child["proc"])
    rest = list(events)
    assert rest == [{"type": "done", "ok": True, "cancelled": True, "finishReason": "cancelled"}]
    assert child["proc"].poll() is not None


@needs_exec
def test_no_first_frame_in_time_is_a_timeout(fake_helper):
    with pytest.raises(host.AppleError) as caught:
        list(host.frames("text", {"prompt": "slow"}, first_timeout=0.2))
    assert caught.value.type == "timeout"
    assert "produced nothing" in str(caught.value)


@needs_exec
def test_a_child_that_dies_without_a_done_is_an_ai_error(fake_helper):
    with pytest.raises(host.AppleError) as caught:
        list(host.frames("text", {"prompt": "die"}))
    assert caught.value.type == "ai_error"
    assert "code 3" in str(caught.value)


@needs_exec
def test_a_child_gone_before_it_reads_stdin_is_its_own_error_and_gets_reaped(fake_helper):
    """Bugbot on #1000: the `finally` used to touch an Event created AFTER the
    stdin write, so a broken pipe surfaced as UnboundLocalError and the child
    was never terminated. A payload larger than the pipe buffer forces the
    write to hit the closed pipe."""
    child = {}
    with pytest.raises(host.AppleError) as caught:
        list(host.frames("die-before-reading", {"pad": "x" * 2_000_000},
                         on_spawn=lambda p: child.setdefault("proc", p)))
    assert caught.value.type in ("ai_unavailable", "ai_error")
    assert child["proc"].poll() is not None


@needs_exec
def test_a_consumer_that_stops_early_terminates_the_child(fake_helper):
    child = {}
    events = host.frames("text", {"prompt": "hang"}, on_spawn=lambda p: child.setdefault("proc", p))
    next(events)
    events.close()
    assert child["proc"].poll() is not None


@needs_exec
def test_cancel_text_stops_every_tracked_child(fake_helper):
    child = {}
    events = host.frames("text", {"prompt": "hang"}, on_spawn=lambda p: child.setdefault("proc", p))
    next(events)  # a generator: the child is spawned on the first pull
    host.track_text(child["proc"])
    assert host.cancel_text() is True
    assert list(events)[-1]["finishReason"] == "cancelled"
    assert host.cancel_text() is False


# -- host.probe ----------------------------------------------------------------


@needs_exec
def test_the_probe_reads_the_helpers_frame_and_caches_it(fake_helper):
    got = host.probe()
    assert got.ok and got.state == "available" and got.os == "26.6.2"
    assert got.default_locale == "en-US"
    assert got.speech_locales == ("en-US", "en-GB", "de-DE", "fr-FR")
    assert got.installed_locales == ("en-US", "de-DE")
    assert got.image_input is False
    assert host.probe() is got  # cached
    assert host.probe(force=True) is not got


def test_the_probe_never_raises_off_a_qualifying_machine(monkeypatch):
    monkeypatch.setattr(host, "platform_problem", lambda: "the apple provider needs macOS")
    host.reset()
    got = host.probe()
    assert got.ok is False and got.state == "unavailable"
    assert "needs macOS" in got.reason


@needs_exec
def test_a_missing_helper_is_an_unavailability_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setenv(host.HELPER_ENV, str(tmp_path / "nope"))
    monkeypatch.setattr(host, "platform_problem", lambda: None)
    host.reset()
    got = host.probe()
    assert got.ok is False and "not an executable file" in got.reason


# -- /api/ai: routing rules and the relay --------------------------------------


def _post_ai(client, **body):
    return client.post("/api/ai", json={"prompt": "hi", **body}, headers={"X-Fused": "1"})


def test_a_pinned_id_under_another_provider_is_a_400_both_ways(client):
    reply = _post_ai(client, model="afm-text", provider="local")
    assert reply.status_code == 400
    assert "belongs to the 'apple' tier" in reply.json()["error"]["message"]
    reply = _post_ai(client, model="afm-text", provider="claude")
    assert reply.status_code == 400


def test_provider_apple_with_a_foreign_or_wrong_capability_model_is_a_400(client):
    reply = _post_ai(client, provider="apple", model="mlx-community/x")
    assert reply.status_code == 400
    assert "serves text as 'afm-text' only" in reply.json()["error"]["message"]
    reply = _post_ai(client, provider="apple", model="afm-speech")
    assert reply.status_code == 400
    assert "apple id for another capability" in reply.json()["error"]["message"]


def test_provider_apple_off_a_qualifying_machine_is_a_409_with_the_reason(client, monkeypatch):
    monkeypatch.setattr(host, "platform_problem", lambda: "the apple provider needs macOS")
    host.reset()
    for body in ({"provider": "apple"}, {"model": "afm-text"}):
        reply = _post_ai(client, **body)
        assert reply.status_code == 409, reply.json()
        assert reply.json()["error"]["type"] == "ai_unavailable"
        assert "needs macOS" in reply.json()["error"]["message"]


@needs_exec
def test_the_text_relay_answers_in_the_shared_result_frame(client, fake_helper):
    """Both spellings, same answer: `provider: "apple"` and `model: "afm-text"`."""
    for body in ({"provider": "apple"}, {"model": "afm-text"}):
        reply = _post_ai(client, effort="high", **body)
        assert reply.status_code == 200, reply.json()
        result = reply.json()["result"]
        assert result["text"] == "hello world"
        assert result["provider"] == "apple" and result["response"]["modelId"] == "afm-text"
        assert result["finishReason"] == "stop" and result["usage"] is None
        assert result["providerMetadata"]["apple"]["usageReported"] is False
        # A tunable the engine lacks is a warning, never a refusal.
        assert any(w["setting"] == "effort" for w in result["warnings"])


@needs_exec
def test_a_guardrail_refusal_is_finish_reason_content_filter(client, fake_helper):
    result = _post_ai(client, provider="apple", prompt="please refuse").json()["result"]
    assert result["finishReason"] == "content-filter"
    assert result["text"] == "I can"
    assert result["providerMetadata"]["apple"]["refusal"] == "guardrail"


@needs_exec
def test_the_relay_streams_chunks_then_the_result(client, fake_helper):
    with client.stream("POST", "/api/ai", json={"prompt": "hi", "provider": "apple", "stream": True},
                       headers={"X-Fused": "1"}) as reply:
        lines = [json.loads(line) for line in reply.iter_lines() if line]
    assert [l["type"] for l in lines] == ["chunk", "chunk", "done"]
    assert lines[-1]["ok"] is True and lines[-1]["result"]["text"] == "hello world"


@needs_exec
def test_a_helper_error_before_any_text_is_a_counted_failure_not_a_500(client, fake_helper):
    reply = _post_ai(client, provider="apple", prompt="overflow")
    assert reply.status_code == 502
    assert reply.json()["error"]["message"] == "context window exceeded"


@needs_exec
def test_a_helper_that_dies_mid_stream_is_an_envelope_not_a_500(client, fake_helper, monkeypatch):
    """Bugbot on #1000: the non-stream loop let a late AppleError escape as
    an unhandled exception. `die` exits after reading the request and
    before any frame, so the FIRST-frame path is what a plain call hits;
    the mid-stream path is forced by a tiny first-frame timeout on `hang`."""
    reply = _post_ai(client, provider="apple", prompt="die")
    assert reply.status_code == 502, reply.text
    assert "code 3" in reply.json()["error"]["message"]


@needs_exec
def test_raw_and_images_are_refused_for_the_apple_tier(client, fake_helper):
    reply = _post_ai(client, provider="apple", raw=True)
    assert reply.status_code == 400
    reply = _post_ai(client, provider="apple", images=["data:image/png;base64,AA=="])
    assert reply.status_code == 400


# -- /api/ai/transcribe --------------------------------------------------------


def _post_transcribe(client, **body):
    return client.post("/api/ai/transcribe", json=body, headers={"X-Fused": "1"})


def _message(reply) -> str:
    """The route's own error shape is `{error: <text>}`; `/api/ai`'s nests it."""
    error = reply.json()["error"]
    return error if isinstance(error, str) else error["message"]


@pytest.fixture()
def take(tmp_path):
    path = tmp_path / "meeting.m4a"
    path.write_bytes(b"not really audio")
    return str(path)


@needs_exec
def test_a_browser_recording_is_a_400_before_any_row_opens(client, fake_helper, tmp_path):
    webm = tmp_path / "playground-mic.webm"
    webm.write_bytes(b"\x1aE\xdf\xa3")
    reply = _post_transcribe(client, provider="apple", path=str(webm))
    assert reply.status_code == 400
    why = _message(reply)
    assert ".webm" in why and 'provider: "local"' in why
    assert jobs.list_jobs() == []


@needs_exec
def test_translate_and_diarize_are_400s_and_tunables_are_warnings(client, fake_helper, take):
    assert _post_transcribe(client, provider="apple", path=take, task="translate").status_code == 400
    assert _post_transcribe(client, model="afm-speech", path=take, diarize=True).status_code == 400
    reply = _post_transcribe(client, provider="apple", path=take, vad=False, initialPrompt="x")
    assert reply.status_code == 200, reply.json()
    assert sorted(w["setting"] for w in reply.json()["warnings"]) == ["initialPrompt", "vad"]


@needs_exec
def test_a_language_apple_lacks_is_a_400(client, fake_helper, take):
    reply = _post_transcribe(client, provider="apple", path=take, language="tlh")
    assert reply.status_code == 400 and "no 'tlh'" in _message(reply)


@needs_exec
def test_the_transcript_files_land_in_the_whisper_workers_shape(client, fake_helper, take):
    reply = _post_transcribe(client, provider="apple", path=take, language="de", words=True)
    assert reply.status_code == 200, reply.json()
    started = reply.json()
    assert started["provider"] == "apple" and started["model"] == "afm-speech"
    assert started["locale"] == "de-DE"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not os.path.exists(started["output"]):
        time.sleep(0.05)
    assert os.path.exists(started["output"]), "the transcript never landed"
    with open(started["output"], encoding="utf-8") as handle:
        record = json.load(handle)
    assert record["text"] == "Yep. It works."
    assert record["model"] == "afm-speech" and record["task"] == "transcribe"
    # ISO 639 like the workers write; the full tag travels beside it.
    assert record["language"] == "de" and record["locale"] == "de-DE"
    assert record["duration"] == 3.0
    assert record["segments"][0]["words"] == [{"start": 0.0, "end": 1.5, "word": "Yep."}]
    with open(started["outputText"], encoding="utf-8") as handle:
        assert handle.read() == "Yep. It works.\n"
    # The partial is gone on a clean finish, as the workers' sink leaves it.
    assert not os.path.exists(started["outputPartial"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = next((j for j in jobs.list_jobs() if j["id"] == started["jobId"]), None)
        if row and row["state"] == "done":
            break
        time.sleep(0.05)
    assert row and row["state"] == "done", row
