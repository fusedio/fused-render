"""The agent backends' vocabularies, pinned against every copy of them.

Each list here is spelled in more than one place because the readers cannot
share an import: the frontend paints its first frame from a hand-kept copy of
the catalog (`CLAUDE_FALLBACK` / `COPILOT_FALLBACK` / `CODEX_FALLBACK` in
frontend/src/platform/lib/agent-backends.ts) before `GET /api/agent-backends`
answers; agent.py is stdlib-only and loaded by path, so it keeps its own
effort and permission-mode tables; and the prefs and tasks routers validate
against their own tuples. A drift between any two is invisible at runtime —
the server row replaces the fallback on the first read, and a value one list
accepts and another refuses only fails the one request that carries it — so
it is caught here instead.
"""
import importlib.util
import os
import re

from fused_render.agent_backends import claude, codex, copilot, registry
from fused_render.server.routers import tasks as tasks_mod
from fused_render.shell import prefs

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TS = os.path.join(_ROOT, "frontend", "src", "platform", "lib", "agent-backends.ts")


def _agent():
    path = os.path.join(_ROOT, "fused_render", "claude_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_pins", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _block(src: str, name: str) -> str:
    """The body of `export const <name>... = { ... };`."""
    m = re.search(r"export const " + name + r"\b[^=]*=\s*\{(.*?)\n\};", src, re.DOTALL)
    assert m, f"no {name} object literal in {_TS}"
    return m.group(1)


def _strings(text: str) -> list[str]:
    return re.findall(r'"([^"]*)"', text)


def _caps(text: str) -> dict:
    return {k: v == "true" for k, v in re.findall(r"(\w+):\s*(true|false)", text)}


def _fallback(src: str, name: str) -> dict:
    """One fallback row as the catalog spells it (`Backend.to_catalog`)."""
    body = _block(src, name)
    field = lambda key: re.search(  # noqa: E731
        r"^\s*" + key + r":\s*(.*?),?\s*$", body, re.MULTILINE).group(1)
    models_m = re.search(r"models:\s*\[(.*?)\]", body, re.DOTALL)
    # `reasoning` is the one per-model fact a fallback row may state (so the
    # effort pill of a model that takes none is hidden from the first paint);
    # absent, the TS type reads it as true, which is what is pinned here.
    models = [{"id": i, "label": label, "reasoning": flag != "false"}
              for i, label, flag in re.findall(
        r'\{\s*id:\s*"([^"]*)",\s*label:\s*"([^"]*)"(?:,\s*reasoning:\s*(true|false))?\s*\}',
        models_m.group(1))]
    caps_src = field("caps")
    if caps_src.startswith("{"):
        caps = _caps(re.search(r"caps:\s*\{(.*?)\}", body, re.DOTALL).group(1))
    else:
        caps = _caps(_block(src, caps_src))
    return {
        "id": _strings(field("id"))[0],
        "label": _strings(field("label"))[0],
        "models": models,
        "default_model": _strings(field("default_model"))[0],
        "efforts": _strings(re.search(r"efforts:\s*\[(.*?)\]", body).group(1)),
        "modes": _strings(re.search(r"modes:\s*\[(.*?)\]", body).group(1)),
        "caps": caps,
    }


def test_the_frontends_fallback_rows_match_the_catalog():
    src = open(_TS, encoding="utf-8").read()
    rows = {r["id"]: r for r in registry.catalog(with_health=False)["all"]}
    keys = ("id", "label", "models", "default_model", "efforts", "modes", "caps")
    for name, backend_id in (("CLAUDE_FALLBACK", "claude"),
                             ("COPILOT_FALLBACK", "copilot"),
                             ("CODEX_FALLBACK", "codex")):
        client = _fallback(src, name)
        server = {k: rows[backend_id][k] for k in keys}
        # Of a model's catalog row the first frame knows id, label and
        # whether it takes an effort; the per-model effort lists, defaults
        # and the facts beside them arrive only with the server's answer.
        server["models"] = [{"id": m["id"], "label": m["label"],
                             "reasoning": m["reasoning"]}
                            for m in server["models"]]
        assert client == server, (
            f"agent-backends.ts {name} has drifted from agent_backends/"
            f"{backend_id}.py — the first frame (and an older server) would "
            "offer a different agent from the one the catalog then serves")


def test_claudes_models_are_the_default_model_prefs_vocabulary():
    assert [m.id for m in claude.MODELS] == [
        m for m in prefs.VALID_DEFAULT_MODELS if m]


def test_every_effort_list_is_one_list():
    agent = _agent()
    assert tuple(claude.EFFORTS) == tuple(copilot.EFFORTS)
    assert tuple(claude.EFFORTS) == tuple(codex.EFFORTS)
    assert tuple(claude.EFFORTS) == tuple(agent._EFFORT_LEVELS)
    assert tuple(claude.EFFORTS) == tuple(
        e for e in tasks_mod._VALID_EFFORTS if e)


def test_copilots_effortless_models_are_agent_pys():
    """agent.py cannot import the package, so it keeps its own copy of the
    Copilot ids `--reasoning-effort` must not ride with."""
    agent = _agent()
    assert tuple(copilot.NO_EFFORT_MODELS) == tuple(agent._COPILOT_NO_EFFORT_MODELS)
    assert set(copilot.NO_EFFORT_MODELS) <= set(copilot.COPILOT.model_ids())


def test_the_permission_modes_are_agent_pys_and_the_others_are_a_subset():
    agent = _agent()
    assert tuple(claude.MODES) == tuple(agent.PERMISSION_MODES)
    assert set(copilot.MODES) <= set(claude.MODES)
    assert set(codex.MODES) <= set(claude.MODES)


def test_every_agent_list_is_one_order():
    """The registry's ORDER, the roots file's BACKENDS and the prefs
    default's vocabulary are three spellings of one list — the PUT's default
    re-point walks the prefs one, the catalog the registry's, the readers the
    roots'. And every backend the registry knows has a switch."""
    from fused_render.agent_backends import roots
    assert registry.ORDER == roots.BACKENDS == prefs.VALID_DEFAULT_BACKENDS
    assert set(prefs.AGENT_BACKEND_KEYS) == set(registry.ORDER)
    for bid in registry.ORDER:
        assert registry.get(bid).pref_key == prefs.AGENT_BACKEND_KEYS[bid]
