"""Every model call an expert makes (docs/experts.md §4 `model.py`).

`text` is a plain completion through the bot's usage-ledgered `_ai_call`.
Every call also writes one line of the expert's own token ledger (`usage.py`).
`tool_turn` is an HTTP client of this server's own `POST /api/ai` with
native tools: it reads the NDJSON stream (`turn`, `chunk`, `tool_call`,
`done`), answers each `tool_call` through `handler(name, input)` and
`POST /api/ai/turns/<turn>/results`, and returns the final text.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from fused_render.experts import store

ENGINES = ("claude", "codex", "copilot")
_NONE_REASON = "needs Claude, Codex or Copilot"


class Unavailable(RuntimeError):
    """No engine this expert may use is turned on."""


def _label(backend_id: str) -> str:
    return {"claude": "Claude", "codex": "Codex", "copilot": "Copilot"}.get(backend_id, backend_id)


#: Engines `auto` may pick. Codex keeps its own shell in a tools turn (`codex exec -s read-only`), so its reads
#: are not fenced by `readtools`: it runs only when the manifest names it.
AUTO_ENGINES = ("claude", "copilot")
#: A sentence per engine the settings page shows beside it ("" for none).
ENGINE_NOTES = {"codex": "On Codex the expert can read files outside its folders. Reads are limited by Codex's "
                         "own read-only sandbox, not by Fused."}
_CLAUDE_FALLBACK = "sonnet"
_CLAUDE_ALIASES = ("haiku", "sonnet", "opus", "fable")


def _claude_model_ok(name: str, backends) -> bool:
    """Whether `name` is a model the Claude engine takes: one of its aliases or a full `claude-…` id. A bot's
    local model (`bots.bot.LOCAL_MODELS`) never is."""
    try:
        from fused_render.bots.bot import LOCAL_MODELS
        if name in LOCAL_MODELS:
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        aliases = {m.id for m in backends.get("claude").models} or set(_CLAUDE_ALIASES)
    except Exception:  # noqa: BLE001
        aliases = set(_CLAUDE_ALIASES)
    return name in aliases or name.lower().startswith("claude-")


def provider_for(bot) -> tuple[str, str]:
    """(provider, model) after resolving `auto` (Claude, else Copilot; never Codex). Raises Unavailable."""
    from fused_render.agent_backends import registry as backends

    man = store.manifest(bot)
    engine = man.get("engine") or "auto"
    try:
        enabled = [b for b in backends.enabled_ids() if b in ENGINES]
    except Exception:  # noqa: BLE001
        enabled = [b for b in ENGINES if _safe_enabled(backends, b)]
    if engine == "auto":
        auto = [b for b in AUTO_ENGINES if b in enabled]
        if auto:
            provider = auto[0]
        elif "codex" in enabled:
            raise Unavailable("needs Claude or Copilot (Codex runs an expert only when chosen in its settings)")
        else:
            raise Unavailable(_NONE_REASON)
    else:
        if engine not in ENGINES:
            raise Unavailable(_NONE_REASON)
        if engine not in enabled:
            raise Unavailable(f"{_label(engine)} is turned off in Preferences")
        provider = engine
    model = (man.get("model") or "").strip()
    if not model and provider == "claude":
        model = str((getattr(bot, "meta", None) or {}).get("model") or "").strip()
        if model and not _claude_model_ok(model, backends):
            model = _CLAUDE_FALLBACK
    if not model:
        try:
            model = backends.get(provider).default_model or ""
        except Exception:  # noqa: BLE001
            model = ""
    return provider, model


def fenced(bot) -> bool:
    """Whether this expert's reads are fenced by `readtools` (its folders only). False on Codex, which reads
    through its own sandbox, and False when no engine resolves."""
    try:
        provider, _model = provider_for(bot)
    except Exception:  # noqa: BLE001 — Unavailable, or a broken registry
        return False
    return provider != "codex"


def _safe_enabled(backends, bid) -> bool:
    try:
        return bool(backends.is_enabled(bid))
    except Exception:  # noqa: BLE001
        return False


def available(bot) -> tuple[bool, str]:
    try:
        provider_for(bot)
    except Unavailable as e:
        return False, str(e) or _NONE_REASON
    except Exception as e:  # noqa: BLE001 — a broken backend registry reads as unavailable
        return False, str(e) or _NONE_REASON
    return True, ""


class _CountingAI:
    """The `fused_ai` client as `bot._ai_call` sees it, but the call goes out as the same non-streaming
    `POST /api/ai` that `fused_ai.text` makes, through the client's own `_post_json`, so the whole result frame
    (its `usage`) is kept on `self.result`. A client without those helpers (a test's fake) is called as is."""

    _KEYS = (("model", "model"), ("provider", "provider"), ("effort", "effort"),
             ("system_prompt", "systemPrompt"), ("thinking", "thinking"))

    def __init__(self, ai):
        self._ai = ai
        self.result = None

    def text(self, prompt, **kw):
        post = getattr(self._ai, "_post_json", None)
        error = getattr(self._ai, "_error_from_payload", None)
        if not callable(post) or not callable(error):
            return self._ai.text(prompt, **kw)
        body = {"prompt": prompt}
        for k, wire in self._KEYS:
            if kw.get(k) is not None:
                body[wire] = kw[k]
        timeout = kw.get("timeout") or getattr(self._ai, "_DEFAULT_TIMEOUT_S", 120.0)
        payload = post("/api/ai", body, timeout=timeout)
        if not payload.get("ok"):
            raise error(200, payload)
        result = payload.get("result") or {}
        self.result = result if isinstance(result, dict) else None
        return (result.get("text") if isinstance(result, dict) else "") or ""


def _account(bot, stage, provider, model_, *, result, chars_in, chars_out, tool_calls, ms, ok):
    """One usage line: the gateway's counts when `result` carries them, else characters / 4. Never raises."""
    try:
        from fused_render.experts import usage
        real = usage.tokens_from_result(result)
        if real is not None:
            usage.record(bot, stage=stage, provider=provider, model=model_, input_tokens=real["input_tokens"],
                         output_tokens=real["output_tokens"], cache_read_tokens=real["cache_read_tokens"],
                         rounds=real["rounds"] if real["rounds"] is not None else max(1, tool_calls),
                         ms=ms, ok=ok, estimated=False)
        else:
            usage.record(bot, stage=stage, provider=provider, model=model_, input_tokens=usage.estimate(chars_in),
                         output_tokens=usage.estimate(chars_out), cache_read_tokens=None,
                         rounds=max(1, tool_calls), ms=ms, ok=ok, estimated=True)
    except Exception:  # noqa: BLE001 — accounting never breaks a model call
        pass


def _stage(stage):
    try:
        from fused_render.experts import usage
        return usage.resolve_stage(stage)
    except Exception:  # noqa: BLE001
        return "other"


def text(bot, prompt, *, system="", timeout=180, stage=None) -> str:
    """One plain completion. `stage` labels its usage line (docs §4 `usage.py`); left out, it is the engine's
    scope or the calling module's name."""
    from fused_render.bots import bot as botmod

    st = _stage(stage)
    provider, model = provider_for(bot)
    kw = {"provider": provider, "model": model or None, "timeout": timeout}
    if system:
        kw["system_prompt"] = system
    ai = _CountingAI(botmod._fused_ai())
    t0 = time.monotonic()
    ok, out = False, ""
    try:
        out = bot._ai_call(ai, prompt, **kw)
        ok = True
        return out
    finally:
        _account(bot, st, provider, model, result=ai.result, chars_in=len(system or "") + len(str(prompt or "")),
                 chars_out=len(out) if isinstance(out, str) else 0, tool_calls=0,
                 ms=int((time.monotonic() - t0) * 1000), ok=ok)


# ---------------------------------------------------------------- tool turns

def _origin() -> str:
    o = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")
    if o:
        return o
    try:
        from fused_render.bots import paths as bpaths
        o = bpaths.server_origin_quiet()
    except Exception:  # noqa: BLE001
        o = ""
    if not o:
        raise RuntimeError("the fused-render server's address is not known (FUSED_RENDER_ORIGIN is unset)")
    return o.rstrip("/")


def _error_sentence(raw: bytes, status) -> str:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
    text_ = raw.decode("utf-8", "replace").strip()
    return text_[:500] or f"the AI request failed (HTTP {status})"


def _post(url, body, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Fused": "1"})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — loopback, our own server


def _answer_call(origin, turn_id, frame, handler, timeout):
    call_id = frame.get("id")
    name = frame.get("name") or ""
    tool_input = frame.get("input") if isinstance(frame.get("input"), dict) else {}
    try:
        content, is_error = handler(name, tool_input)
        content = content if isinstance(content, str) else json.dumps(content, default=str)
        is_error = bool(is_error)
    except Exception as e:  # noqa: BLE001 — a failed tool is the model's to read, never the turn's crash
        content, is_error = f"error: {type(e).__name__}: {e}", True
    if not turn_id or not call_id:
        return content
    url = f"{origin}/api/ai/turns/{turn_id}/results"
    try:
        with _post(url, {"id": call_id, "content": content, "isError": is_error},
                   min(timeout, 60)) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        # 404/409: the turn ended or no longer waits on this call; the stream says what happened.
        try:
            e.read()
        except Exception:  # noqa: BLE001
            pass
    except (OSError, ValueError):
        pass
    return content


def _ledger(bot, model, ok, err):
    try:
        from fused_render.bots import store as bstore
        meta = getattr(bot, "meta", None) or {}
        bstore.usage_log(bot.id, model or "", getattr(bot, "task_origin", "manual"), meta.get("task"),
                         ok, name=meta.get("name"), error=err)
    except Exception:  # noqa: BLE001
        pass


def tool_turn(bot, prompt, *, system, tools, handler, timeout=600, stage=None) -> str:
    """One `/api/ai` tools turn. Returns the final text; RuntimeError with the
    server's sentence on any failure (Unavailable when no engine is on). `stage`
    labels the usage line, as for `text`."""
    st = _stage(stage)
    provider, model = provider_for(bot)
    origin = _origin()
    body = {"prompt": prompt, "stream": True, "tools": list(tools or []), "provider": provider}
    if model:
        body["model"] = model
    if system:
        body["systemPrompt"] = system
    t0 = time.monotonic()
    deadline = t0 + max(1.0, float(timeout))
    ok, err, final = False, None, None
    # For the estimate when the gateway reports no counts: every model round re-sends the system prompt, the
    # prompt and every tool result so far, so round i's input is base + the results before it.
    acct = {"calls": 0, "in": 0, "out": 0, "ctx": 0, "result": None}
    base = len(system or "") + len(str(prompt or ""))

    def answered(frame):
        content = _answer_call(origin, turn_id, frame, handler, timeout)
        try:
            call_chars = len(json.dumps(frame.get("input"), default=str))
        except Exception:  # noqa: BLE001
            call_chars = 0
        acct["in"] += base + acct["ctx"]
        acct["calls"] += 1
        acct["out"] += call_chars
        acct["ctx"] += call_chars + len(content if isinstance(content, str) else "")
    try:
        try:
            resp = _post(f"{origin}/api/ai", body, timeout)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001
                raw = b""
            raise RuntimeError(_error_sentence(raw, e.code)) from None
        except (OSError, ValueError) as e:
            raise RuntimeError(f"could not reach the AI gateway: {e}") from None
        turn_id, chunks, final = "", [], None
        with resp:
            status = getattr(resp, "status", 200)
            if status and not (200 <= int(status) < 300):
                raise RuntimeError(_error_sentence(resp.read(), status))
            while True:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"the AI turn did not finish within {int(timeout)}s")
                try:
                    line = resp.readline()
                except (OSError, ValueError) as e:
                    raise RuntimeError(f"the AI stream broke: {e}") from None
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                kind = frame.get("type")
                if kind == "turn":
                    turn_id = str(frame.get("id") or "")
                elif kind == "chunk":
                    chunks.append(str(frame.get("text") or ""))
                elif kind == "tool_call":
                    answered(frame)
                elif kind == "done":
                    if not frame.get("ok", False):
                        e = frame.get("error")
                        msg = (e.get("message") if isinstance(e, dict) else e) or "the AI turn failed"
                        raise RuntimeError(str(msg))
                    result = frame.get("result")
                    acct["result"] = result
                    if isinstance(result, dict) and isinstance(result.get("text"), str):
                        final = result["text"]
                    else:
                        final = "".join(chunks)
                    break
                elif kind == "error":
                    e = frame.get("error")
                    msg = (e.get("message") if isinstance(e, dict) else e) or frame.get("message")
                    raise RuntimeError(str(msg or "the AI turn failed"))
        if final is None:
            raise RuntimeError("the AI stream ended without an answer")
        ok = True
        return final
    except RuntimeError as e:
        err = str(e)
        raise
    finally:
        _ledger(bot, model, ok, err)
        out_chars = acct["out"] + (len(final) if ok and isinstance(final, str) else 0)
        _account(bot, st, provider, model, result=acct["result"], chars_in=acct["in"] + base + acct["ctx"],
                 chars_out=out_chars, tool_calls=acct["calls"], ms=int((time.monotonic() - t0) * 1000), ok=ok)
