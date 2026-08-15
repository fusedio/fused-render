"""Tests for the Preferences backend (SPEC §20): GET/PUT /api/prefs
(shell/prefs.py — the persisted engine/deploy/reader/call-log preferences), the
per-request engine dispatch it drives in /api/run, and the merged
extension→templates registry view (GET /api/templates/registry).

FUSED_RENDER_HOME is redirected to a tmp dir and FUSED_RENDER_ENGINE cleared
so no test reads the real prefs or a developer's env override.
"""
import json
import os

from fastapi.testclient import TestClient

import fused_render.shell.prefs as prefs_mod
from fused_render.server import create_app


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app), home


# -- /api/prefs -----------------------------------------------------------------


def test_defaults_to_fused_when_available_unforced(tmp_path, monkeypatch):
    """D204 flipped the unset-pref default from builtin to fused-when-available.
    `fused_available` is stubbed rather than left to the test environment: the
    default's whole point is that it depends on the environment, so a test that
    let the environment answer would assert nothing on the machine that lacks the
    package."""
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    body = client.get("/api/prefs").json()
    assert body["engine"]["selected"] == "fused"
    assert body["engine"]["effective"] == "fused"
    assert body["engine"]["forced_by"] is None
    assert body["engine"]["fused_available"] is True
    # The app's own log left this payload with its Preferences section
    # (PF-5): absence asserted so it cannot quietly come back.
    assert "log" not in body


def test_an_unset_pref_still_runs_builtin_while_fused_is_missing(tmp_path,
                                                                monkeypatch):
    """The half of D204 that makes it safe: `effective_engine` ANDs the selection
    with live availability, so "fused by default" cannot mean "broken by default"
    on a machine without the package. Pinned separately from the flip because it is
    the property that must survive it."""
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: False)
    body = client.get("/api/prefs").json()
    assert body["engine"]["selected"] == "fused"      # nothing stored
    assert body["engine"]["effective"] == "builtin"   # ...but nothing to run it
    assert body["engine"]["fused_available"] is False
    # The page reads exactly this pair to show "Currently running: Local
    # (built-in) — falling back while the fused package is unavailable".
    assert client.get("/api/config").json()["engine"] == "builtin"


def test_a_stored_builtin_still_pins_builtin(tmp_path, monkeypatch):
    """The flip is to the DEFAULT only. A user who chose builtin chose it, and an
    available fused package must not quietly override that — which is the exact
    surprise D70 was about."""
    client, home = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    body = client.put("/api/prefs", json={"engine": "builtin"}, headers=FUSED).json()
    assert json.loads((home / "prefs.json").read_text(encoding="utf-8"))["engine"] \
        == "builtin"
    assert body["engine"]["selected"] == "builtin"
    assert body["engine"]["effective"] == "builtin"
    assert client.get("/api/prefs").json()["engine"]["effective"] == "builtin"


def test_put_persists_and_degrades_while_fused_unavailable(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: False)
    body = client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED).json()
    # Persisted...
    saved = json.loads((home / "prefs.json").read_text(encoding="utf-8"))
    assert saved["engine"] == "fused"
    assert body["engine"]["selected"] == "fused"
    # ...but effective degrades to builtin until the package is importable.
    assert body["engine"]["effective"] == "builtin"

    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    assert client.get("/api/prefs").json()["engine"]["effective"] == "fused"


def test_put_rejects_unknown_engine_and_missing_header(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    assert client.put("/api/prefs", json={"engine": "warp"}, headers=FUSED).status_code == 400
    assert client.put("/api/prefs", json={"engine": "fused"}).status_code == 403
    assert not (home / "prefs.json").exists()


def test_deploy_enabled_defaults_off_and_toggles(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Default off (opt-in), so the preview-header Deploy button stays hidden.
    assert client.get("/api/prefs").json()["deploy"]["enabled"] is False
    # Turn it on — persisted and reflected in the response and a fresh GET.
    body = client.put("/api/prefs", json={"deploy_enabled": True}, headers=FUSED).json()
    assert body["deploy"]["enabled"] is True
    assert json.loads((home / "prefs.json").read_text(encoding="utf-8"))["deploy_enabled"] is True
    assert client.get("/api/prefs").json()["deploy"]["enabled"] is True
    # And back off.
    assert client.put("/api/prefs", json={"deploy_enabled": False}, headers=FUSED).json()[
        "deploy"
    ]["enabled"] is False


def test_deploy_enabled_toggle_is_independent_of_engine(tmp_path, monkeypatch):
    # A partial PUT touching only deploy_enabled must not disturb the engine pref.
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED)
    body = client.put("/api/prefs", json={"deploy_enabled": True}, headers=FUSED).json()
    assert body["engine"]["selected"] == "fused"
    assert body["deploy"]["enabled"] is True


def test_reader_enabled_defaults_off_and_toggles(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Default off (accessibility opt-in), so the reader gate denies the mode.
    assert client.get("/api/prefs").json()["reader"]["enabled"] is False
    # Turn it on — persisted and reflected in the response and a fresh GET.
    body = client.put("/api/prefs", json={"reader_enabled": True}, headers=FUSED).json()
    assert body["reader"]["enabled"] is True
    assert json.loads((home / "prefs.json").read_text(encoding="utf-8"))["reader_enabled"] is True
    assert client.get("/api/prefs").json()["reader"]["enabled"] is True
    # And back off.
    assert client.put("/api/prefs", json={"reader_enabled": False}, headers=FUSED).json()[
        "reader"
    ]["enabled"] is False


def test_reader_enabled_toggle_is_independent_of_other_prefs(tmp_path, monkeypatch):
    # A partial PUT touching only reader_enabled must not disturb the others.
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED)
    client.put("/api/prefs", json={"deploy_enabled": True}, headers=FUSED)
    body = client.put("/api/prefs", json={"reader_enabled": True}, headers=FUSED).json()
    assert body["engine"]["selected"] == "fused"
    assert body["deploy"]["enabled"] is True
    assert body["reader"]["enabled"] is True


def test_put_rejects_bad_reader_enabled(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Non-boolean reader_enabled is rejected without a write.
    assert (
        client.put("/api/prefs", json={"reader_enabled": "yes"}, headers=FUSED).status_code == 400
    )
    assert not (home / "prefs.json").exists()


def test_put_rejects_bad_deploy_enabled_and_empty_body(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Non-boolean deploy_enabled …
    assert (
        client.put("/api/prefs", json={"deploy_enabled": "yes"}, headers=FUSED).status_code == 400
    )
    # … and a PUT naming no known preference are both rejected without a write.
    assert client.put("/api/prefs", json={"nope": 1}, headers=FUSED).status_code == 400
    assert not (home / "prefs.json").exists()


def test_default_model_defaults_to_unset_and_round_trips(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Unset is its own value — "" means "whatever each consumer's own default
    # is", not a model. Every consumer treats it as no answer at all.
    assert client.get("/api/prefs").json()["model"]["default"] == ""
    body = client.put("/api/prefs", json={"default_model": "opus"}, headers=FUSED).json()
    assert body["model"]["default"] == "opus"
    stored = json.loads((home / "prefs.json").read_text(encoding="utf-8"))
    assert stored["default_model"] == "opus"
    assert client.get("/api/prefs").json()["model"]["default"] == "opus"
    # And back to unset, which is a settable choice and not just an absence.
    assert (
        client.put("/api/prefs", json={"default_model": ""}, headers=FUSED).json()["model"][
            "default"
        ]
        == ""
    )


def test_default_model_accepts_exactly_the_short_names(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    # The short names are the claude template's own selector list — the
    # preference has to speak the same vocabulary as the control it presets.
    for name in ("fable", "opus", "sonnet", "haiku"):
        assert (
            client.put("/api/prefs", json={"default_model": name}, headers=FUSED).json()["model"][
                "default"
            ]
            == name
        )


def test_put_rejects_an_unknown_default_model(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # A full API id is NOT accepted: the pref is the short name, and the
    # short→id mapping lives in exactly one place (server/ai.py).
    for bad in ("claude-opus-5", "gpt-4", 3, None):
        assert (
            client.put("/api/prefs", json={"default_model": bad}, headers=FUSED).status_code == 400
        )
    assert not (home / "prefs.json").exists()


def test_default_model_is_independent_of_the_other_prefs(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED)
    client.put("/api/prefs", json={"reader_enabled": True}, headers=FUSED)
    body = client.put("/api/prefs", json={"default_model": "haiku"}, headers=FUSED).json()
    assert body["engine"]["selected"] == "fused"
    assert body["reader"]["enabled"] is True
    assert body["model"]["default"] == "haiku"


def test_default_model_reader_ignores_a_hand_edited_junk_value(tmp_path, monkeypatch):
    # prefs.json is a user-editable file; an unknown value reads as unset
    # rather than reaching a consumer that would pass it to a CLI.
    client, home = _client(tmp_path, monkeypatch)
    home.mkdir(parents=True, exist_ok=True)
    (home / "prefs.json").write_text(json.dumps({"default_model": "wat"}), encoding="utf-8")
    assert prefs_mod.default_model() == ""
    assert client.get("/api/prefs").json()["model"]["default"] == ""


def test_env_var_reports_as_forcing(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "builtin")
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    # The pref still persists (applies once the override is removed), but the
    # reported state says who is in charge right now.
    body = client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED).json()
    assert body["engine"]["forced_by"] == "builtin"
    assert body["engine"]["effective"] == "builtin"


def test_forced_auto_reports_match_dispatch_after_midsession_install(tmp_path, monkeypatch):
    # FUSED_RENDER_ENGINE=auto with fused absent at startup. The engine must be
    # resolved LIVE, so a mid-session install (which /api/deploy/install
    # supports) flips BOTH the reported state and actual dispatch together —
    # the page never claims a different running engine than /api/run uses.
    # (Built here, not via _client, since _client clears FUSED_RENDER_ENGINE
    # and create_app must see =auto at startup.)
    available = {"v": False}
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "auto")
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: available["v"])
    client = TestClient(create_app(start_dir=str(tmp_path)))  # validates =auto (no raise)

    assert client.get("/api/config").json()["engine"] == "builtin"
    assert client.get("/api/prefs").json()["engine"]["effective"] == "builtin"

    available["v"] = True  # installed mid-session
    assert client.get("/api/config").json()["engine"] == "fused"
    assert client.get("/api/prefs").json()["engine"]["effective"] == "fused"


# -- per-request engine dispatch (server /api/run + /api/config) -----------------


async def _fake_fused_run(path, params):
    return {"ok": True, "result": {"engine": "fused-stub"}, "stdout": ""}


def test_engine_switch_applies_without_restart(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    (tmp_path / "one.py").write_text("def main():\n    return {'engine': 'builtin-real'}\n", encoding="utf-8")

    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    monkeypatch.setattr("fused_render.engine.run_python", _fake_fused_run, raising=False)

    # Default pref, with the package importable: the fused engine (D204).
    assert client.get("/api/config").json()["engine"] == "fused"
    run = client.post(
        "/api/run", json={"py": str(tmp_path / "one.py"), "params": {}}, headers=FUSED
    ).json()
    assert run["result"] == {"engine": "fused-stub"}

    # Flip the pref — the SAME app instance dispatches the next run to the
    # built-in executor (no restart), and /api/config reports it.
    client.put("/api/prefs", json={"engine": "builtin"}, headers=FUSED)
    assert client.get("/api/config").json()["engine"] == "builtin"
    run = client.post(
        "/api/run", json={"py": str(tmp_path / "one.py"), "params": {}}, headers=FUSED
    ).json()
    assert run["result"] == {"engine": "builtin-real"}

    # And back.
    client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED)
    run = client.post(
        "/api/run", json={"py": str(tmp_path / "one.py"), "params": {}}, headers=FUSED
    ).json()
    assert run["result"] == {"engine": "fused-stub"}


def test_forced_env_var_beats_the_pref(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "builtin")
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED)
    # The process override pins the engine regardless of the pref.
    assert client.get("/api/config").json()["engine"] == "builtin"


# -- /api/templates/registry ------------------------------------------------------


def _point_user_registry_at(tmp_path, monkeypatch):
    # USER_REGISTRY/USER_TEMPLATES_DIR are module constants resolved at import
    # (same seam test_templates.py patches).
    from fused_render.server import templates as _server_templates

    udir = tmp_path / "user-templates"
    udir.mkdir()
    monkeypatch.setattr(_server_templates, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(_server_templates, "USER_REGISTRY", str(udir / "registry.json"))
    return udir


def _names(entry):
    # The effective ordered template names for an entry (§2.2 templates are now
    # {name, source, exists, hasIcon} objects, not bare strings).
    return [t["name"] for t in entry["templates"]]


def test_registry_view_lists_builtin_bindings(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    _point_user_registry_at(tmp_path, monkeypatch)
    body = client.get("/api/templates/registry").json()
    # The sources block is modelled for extensibility (§1) — core + user today.
    assert {s["id"] for s in body["sources"]} == {"core", "user"}
    by_key = {e["key"]: e for e in body["entries"]}
    html = by_key[".html"]
    # Pin the contract, not the whole shipped list: rendered-first default.
    assert _names(html)[:2] == ["_render", "code"]
    assert html["resolvedSource"] == "core"
    assert html["overridesCore"] is False
    assert html["keyKind"] == "simple"
    parquet = by_key[".parquet"]
    assert _names(parquet)[0] == "duckdb"
    # Per-template objects carry the resolved source + icon presence.
    first = parquet["templates"][0]
    assert first["source"] == "core" and first["exists"] is True and first["hasIcon"] is True
    # Directory keys sort after file keys and keep their trailing slash.
    assert body["entries"][-1]["key"].endswith("/")
    assert by_key[".zarr/"]["keyKind"] == "directory"
    assert body["error"] is None


def test_registry_view_shows_user_bindings_and_overrides(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    (udir / "registry.json").write_text(
        json.dumps(
            {
                ".html": ["code"],  # override: drop the rendered mode
                ".xyz": ["tree"],  # new user-only binding
                ".log": None,  # disabled: no preview at all
            }
        ),
        encoding="utf-8",
    )
    body = client.get("/api/templates/registry").json()
    by_key = {e["key"]: e for e in body["entries"]}

    html = by_key[".html"]
    assert html["resolvedSource"] == "user"
    assert html["overridesCore"] is True
    assert _names(html) == ["code"]
    assert html["userValue"] == ["code"]
    assert html["coreTemplates"][:2] == ["_render", "code"]  # what a reset restores

    xyz = by_key[".xyz"]
    assert xyz["resolvedSource"] == "user"
    assert xyz["overridesCore"] is True  # the user registry defines this key
    assert _names(xyz) == ["tree"]
    assert xyz["coreTemplates"] is None  # builtin has no .xyz key

    log = by_key[".log"]
    assert log["disabled"] is True
    assert log["templates"] == []
    assert log["userValue"] is None
    # One row per key: the built-in .html entry is replaced, not doubled.
    assert sum(1 for e in body["entries"] if e["key"] == ".html") == 1


def test_registry_view_splice_token_is_dangling(tmp_path, monkeypatch):
    # Splice removed: "..." is an ordinary name kept as broken (exists:false)
    # in the row, not expanded to the built-in list.
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    (udir / "registry.json").write_text(
        json.dumps({".html": ["...", "tree"]}), encoding="utf-8"
    )
    body = client.get("/api/templates/registry").json()
    by_key = {e["key"]: e for e in body["entries"]}
    tmpl = {t["name"]: t for t in by_key[".html"]["templates"]}
    assert "..." in tmpl and tmpl["..."]["exists"] is False
    assert _names(by_key[".html"]) == ["...", "tree"]  # verbatim, unexpanded
    assert by_key[".html"]["error"] is None  # dangling name is not a shape error


def test_registry_view_override_is_case_insensitive(tmp_path, monkeypatch):
    # Resolution matches keys case-insensitively (_key_segments lowercases), so
    # a user ".CSV" key OVERRIDES the built-in ".csv" — the view must show ONE
    # row sourced user, not two rows (a case-sensitive `in` check would
    # double-list the key and mis-source both).
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    (udir / "registry.json").write_text(json.dumps({".CSV": ["code"]}), encoding="utf-8")
    entries = client.get("/api/templates/registry").json()["entries"]
    csv_rows = [e for e in entries if e["key"].lower() == ".csv"]
    assert len(csv_rows) == 1
    assert csv_rows[0]["key"] == ".CSV"
    assert csv_rows[0]["resolvedSource"] == "user"
    assert csv_rows[0]["overridesCore"] is True
    assert _names(csv_rows[0]) == ["code"]


# -- the call store's existence (Bugbot #283 review, D148) ----------------------


def test_calls_dir_exists_is_false_before_the_first_record(tmp_path, monkeypatch):
    """The `Browse call logs` affordance is gated on this flag.

    The writer creates the store on its first append, so between "capture on"
    and "a page actually called something" `dir` names a path that is not
    there. Reported rather than created here: this is a GET, and a read that
    provisions storage puts the side effect in the wrong place.
    """
    client, _ = _client(tmp_path, monkeypatch)
    calls = client.get("/api/prefs").json()["calls"]

    assert calls["dir_exists"] is False
    assert not os.path.exists(calls["dir"]), "a GET of the prefs must not create the store"


def test_calls_dir_exists_flips_once_a_record_lands(tmp_path, monkeypatch):
    from fused_render import calls as call_log

    client, _ = _client(tmp_path, monkeypatch)
    assert client.get("/api/prefs").json()["calls"]["dir_exists"] is False

    call_log._append([{"version": 1, "call_id": "c1", "kind": "call",
                       "occurred_at": call_log._now_iso(), "page": "/app/p.html"}])

    body = client.get("/api/prefs").json()["calls"]
    assert body["dir_exists"] is True
    assert os.path.isdir(body["dir"])


# -- env overrides are surfaced, not hidden (Bugbot #283 review, D149) ---------


def test_calls_effective_state_matches_the_stored_prefs_when_unforced(tmp_path, monkeypatch):
    """The common case: nothing forced, so `effective_*` echoes the stored prefs
    and `*_forced_by` is null. Asserted so a future change can't quietly make
    the effective pair diverge from the writer when no override is present."""
    monkeypatch.delenv("FUSED_RENDER_CALLS", raising=False)
    monkeypatch.delenv("FUSED_RENDER_CALLS_RETENTION_DAYS", raising=False)
    # `calls._prefs_cache` is a module-global with a TTL, so deleting the env vars
    # above is not enough: a warm entry a test earlier in this xdist worker left
    # behind (populated WHILE an override was set — and `store`'s monkeypatched
    # reset restores the pre-test value on teardown, so it can even be
    # resurrected) answers here from the old verdict and `effective_enabled`
    # diverges from the stored pref. The sibling test below already invalidates
    # for exactly this reason; this one did not, which made it fail only on
    # whichever worker happened to schedule a calls test before it. Found while
    # merging — it is an order-dependent flake in the suite, not a merge defect.
    from fused_render import calls as call_log
    call_log.invalidate_prefs_cache()
    client, _ = _client(tmp_path, monkeypatch)

    calls = client.get("/api/prefs").json()["calls"]
    assert calls["effective_enabled"] == calls["enabled"]
    assert calls["effective_retention_days"] == calls["retention_days"]
    assert calls["enabled_forced_by"] is None
    assert calls["retention_forced_by"] is None


def test_calls_env_overrides_are_reported_against_the_stored_prefs(tmp_path, monkeypatch):
    """The bug: the page showed capture on and 90-day retention while the
    process had capture off and a 1-day window.

    Both halves are asserted — the stored prefs still say what the user chose
    (a PUT must round-trip, and the choice applies once the var is removed),
    and `effective_*` says what is actually happening.
    """
    from fused_render import calls as call_log

    client, _ = _client(tmp_path, monkeypatch)
    client.put("/api/prefs", json={"calls_enabled": True}, headers=FUSED)
    client.put("/api/prefs", json={"calls_retention_days": 90}, headers=FUSED)

    monkeypatch.setenv("FUSED_RENDER_CALLS", "0")
    monkeypatch.setenv("FUSED_RENDER_CALLS_RETENTION_DAYS", "1")
    call_log.invalidate_prefs_cache()

    calls = client.get("/api/prefs").json()["calls"]
    assert calls["enabled"] is True and calls["retention_days"] == 90, "the stored choice stands"
    assert calls["effective_enabled"] is False
    assert calls["effective_retention_days"] == 1
    assert calls["enabled_forced_by"] == "0"
    assert calls["retention_forced_by"] == "1"


def test_calls_effective_state_comes_from_the_writers_own_resolvers(tmp_path, monkeypatch):
    """Pins the effective pair to `calls.enabled()`/`calls.retention_days()` —
    the functions the writer itself calls — rather than to a second copy of the
    precedence rule in the prefs layer, which is how the two would drift.

    The env var here is deliberately a spelling only the real resolver accepts
    (`enabled()` treats any non-false-ish value as on, so "off" is off but
    "anything" is on); a reimplementation checking `== "0"` would disagree.
    """
    from fused_render import calls as call_log

    client, _ = _client(tmp_path, monkeypatch)
    for raw in ("off", "no", "false", "0"):
        monkeypatch.setenv("FUSED_RENDER_CALLS", raw)
        call_log.invalidate_prefs_cache()
        body = client.get("/api/prefs").json()["calls"]
        assert body["effective_enabled"] is call_log.enabled(), raw
        assert body["effective_enabled"] is False, raw

    monkeypatch.setenv("FUSED_RENDER_CALLS", "1")
    call_log.invalidate_prefs_cache()
    body = client.get("/api/prefs").json()["calls"]
    assert body["effective_enabled"] is call_log.enabled() is True


def test_calls_params_mode_gets_no_forced_by_pair(tmp_path, monkeypatch):
    """Only capture and retention have env overrides. The param mode gets no
    pair rather than an always-null one that would imply an override exists."""
    client, _ = _client(tmp_path, monkeypatch)
    calls = client.get("/api/prefs").json()["calls"]

    assert "params" in calls
    assert "params_forced_by" not in calls
    assert "effective_params" not in calls


# -- forced_by means "in force", not "set" (Bugbot #283 review, D150) ----------


def test_retention_forced_by_is_null_when_the_env_value_is_not_honoured(tmp_path, monkeypatch):
    """The bug: any *set* FUSED_RENDER_CALLS_RETENTION_DAYS was reported as
    forcing, but `retention_days()` honours only a non-empty integer.

    An empty or non-numeric value left the writer on the stored pref while the
    page disabled the retention control and blamed the variable — a control the
    user then could not change from the page, and a variable whose value was
    never in force. `forced_by` must answer "is this in force", not "is this
    set", so each spelling below reports null and the stored window stands.
    """
    from fused_render import calls as call_log

    client, _ = _client(tmp_path, monkeypatch)
    client.put("/api/prefs", json={"calls_retention_days": 30}, headers=FUSED)

    for raw in ("", "abc", "-", "3.5", "  "):
        monkeypatch.setenv("FUSED_RENDER_CALLS_RETENTION_DAYS", raw)
        call_log.invalidate_prefs_cache()
        calls = client.get("/api/prefs").json()["calls"]
        assert calls["retention_forced_by"] is None, raw
        assert calls["effective_retention_days"] == call_log.retention_days() == 30, raw


def test_retention_forced_by_is_reported_when_the_env_value_wins(tmp_path, monkeypatch):
    """The other side of the same rule: a value the resolver does honour — `0`
    included, which is a real override (it disables age pruning) and must not be
    mistaken for the falsy empty string — is reported and does lock the UI."""
    from fused_render import calls as call_log

    client, _ = _client(tmp_path, monkeypatch)
    client.put("/api/prefs", json={"calls_retention_days": 30}, headers=FUSED)

    for raw, expected in (("7", 7), ("0", 0), ("-5", 0)):
        monkeypatch.setenv("FUSED_RENDER_CALLS_RETENTION_DAYS", raw)
        call_log.invalidate_prefs_cache()
        calls = client.get("/api/prefs").json()["calls"]
        assert calls["retention_forced_by"] == raw
        assert calls["effective_retention_days"] == call_log.retention_days() == expected
        assert calls["retention_days"] == 30, "the stored choice stands"


def test_forced_by_flags_track_the_writers_override_resolvers(tmp_path, monkeypatch):
    """Pins both `*_forced_by` flags to `calls.*_override()` — the same
    ask-the-writer discipline the `effective_*` values follow (D149).

    Presence and force coincide for capture (every set value decides something)
    but not for retention, so a presence check would look right on one control
    and be wrong on the other. Asserting against the resolvers keeps the two in
    step whichever way a future rule change moves.
    """
    from fused_render import calls as call_log

    client, _ = _client(tmp_path, monkeypatch)
    for capture, retention in (("0", "7"), ("", "abc"), ("1", ""), ("no", "0")):
        monkeypatch.setenv("FUSED_RENDER_CALLS", capture)
        monkeypatch.setenv("FUSED_RENDER_CALLS_RETENTION_DAYS", retention)
        call_log.invalidate_prefs_cache()
        calls = client.get("/api/prefs").json()["calls"]

        expected_capture = capture if call_log.enabled_override() is not None else None
        expected_retention = retention if call_log.retention_days_override() is not None else None
        assert calls["enabled_forced_by"] == expected_capture, (capture, retention)
        assert calls["retention_forced_by"] == expected_retention, (capture, retention)


# -- the inference engine preference (D301) -------------------------------------
#
# Driven through the ENDPOINT, because what is under test here is the STORE: what
# a PUT accepts, what it refuses, what a GET reports back, and what a stored
# value does to a resident model. WHICH runner a preference resolves to, and what
# happens to one this machine cannot honour, is `tests/test_ai_runtime.py`'s.


def test_the_engines_payload_starts_at_auto_for_every_capability(tmp_path, monkeypatch):
    """The default is "the registry decides", which is exactly today's
    behaviour — the feature has to be invisible until somebody uses it."""
    from fused_render.ai import registry

    client, _ = _client(tmp_path, monkeypatch)
    engines = client.get("/api/prefs").json()["engines"]

    assert engines["auto"] == "auto"
    rows = {row["capability"]: row for row in engines["capabilities"]}
    assert set(rows) == set(registry.capabilities())
    assert all(row["selected"] == "auto" for row in rows.values())
    # Every capability carries its own choices, each with the availability
    # reason a disabled control shows — the page writes none of this copy.
    for row in rows.values():
        assert row["choices"], row
        assert all("available" in choice for choice in row["choices"])


def test_the_auto_literal_agrees_with_the_registrys():
    """Spelled in both modules rather than imported (prefs is on `calls.py`'s
    hot path and must not drag the AI registry in behind it). A drift would read
    as a preference for a runner named "auto" and be dropped as unknown."""
    from fused_render.ai import registry

    assert prefs_mod.AUTO_ENGINE == registry.AUTO


def test_an_engine_choice_round_trips_and_persists(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    body = client.put(
        "/api/prefs",
        json={"engines": {"automatic-speech-recognition": "faster-whisper"}},
        headers=FUSED).json()

    rows = {row["capability"]: row for row in body["engines"]["capabilities"]}
    assert rows["automatic-speech-recognition"]["selected"] == "faster-whisper"
    stored = json.loads((home / "prefs.json").read_text())
    assert stored["engines"] == {"automatic-speech-recognition": "faster-whisper"}
    # The PUT's reply is the new state, so the page re-renders from it rather
    # than re-fetching — the two must be the same answer.
    assert (client.get("/api/prefs").json()["engines"]["capabilities"]
            == body["engines"]["capabilities"])


def test_setting_one_capabilitys_engine_leaves_the_others_alone(tmp_path, monkeypatch):
    """The partial-update rule this handler follows everywhere else, applied one
    level down: the page changes one capability and must not have to echo the
    rest, or two open tabs would each undo the other."""
    client, home = _client(tmp_path, monkeypatch)
    client.put("/api/prefs", json={"engines": {"text-generation": "transformers-text"}},
               headers=FUSED)
    client.put("/api/prefs",
               json={"engines": {"automatic-speech-recognition": "mlx-whisper"}},
               headers=FUSED)

    stored = json.loads((home / "prefs.json").read_text())
    assert stored["engines"] == {"text-generation": "transformers-text",
                                 "automatic-speech-recognition": "mlx-whisper"}


def test_auto_is_a_value_you_can_write_BACK(tmp_path, monkeypatch):
    """Undo has to be reachable. "Auto" is a choice the control offers, so it
    has to be a choice the endpoint accepts — not merely the absence of a key,
    which the page has no way to send."""
    client, _ = _client(tmp_path, monkeypatch)
    client.put("/api/prefs",
               json={"engines": {"automatic-speech-recognition": "mlx-whisper"}},
               headers=FUSED)
    body = client.put("/api/prefs",
                      json={"engines": {"automatic-speech-recognition": "auto"}},
                      headers=FUSED).json()
    rows = {row["capability"]: row for row in body["engines"]["capabilities"]}
    assert rows["automatic-speech-recognition"]["selected"] == "auto"


def test_a_preference_this_MACHINE_cannot_honour_is_still_stored(tmp_path, monkeypatch):
    """Legal is a weaker claim than usable, deliberately.

    A user with a Mac and a Windows box shares one prefs.json through a synced
    home directory. Refusing to STORE a choice the current machine cannot run
    would make the file un-shareable, and rewriting it on read would make the
    choice un-restorable. The resolution is what drops it, per machine, with the
    reason shown — so this asserts the trio: stored as chosen, not in force, and
    said out loud.
    """
    from fused_render.ai import registry

    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    client, _ = _client(tmp_path, monkeypatch)

    body = client.put(
        "/api/prefs",
        json={"engines": {"automatic-speech-recognition": "mlx-whisper"}},
        headers=FUSED).json()
    speech = {row["capability"]: row
              for row in body["engines"]["capabilities"]}["automatic-speech-recognition"]
    assert speech["selected"] == "mlx-whisper"
    assert speech["effective"] == "faster-whisper"
    assert "Apple Silicon" in speech["ignoredReason"]


def test_a_MEANINGLESS_engine_choice_is_refused(tmp_path, monkeypatch):
    """What can never be honoured on ANY machine is refused at the door: an
    unknown capability, an unknown runner, or a runner paired with a capability
    it does not serve. Those are not choices with a story to tell — there is
    nothing for a page to explain about them, and nothing a different machine
    would make true."""
    client, home = _client(tmp_path, monkeypatch)
    for payload in ({"automatic-speech-recognition": "whisper-9000"},
                    {"speech-to-text": "faster-whisper"},
                    {"text-generation": "faster-whisper"},
                    {"automatic-speech-recognition": 7},
                    "faster-whisper"):
        response = client.put("/api/prefs", json={"engines": payload}, headers=FUSED)
        assert response.status_code == 400, payload
    assert not (home / "prefs.json").exists(), "a refused PUT wrote nothing"


def test_changing_an_engine_EVICTS_the_resident_model_for_that_capability(
        tmp_path, monkeypatch):
    """One capability holds one resident model, and it belongs to the backend
    that loaded it — a Whisper model resident in the CTranslate2 worker is not
    usable by the MLX one. Left alone it would hold gigabytes for a runner
    nothing routes to again, show as that capability's resident model, and have
    the next transcription start a second worker beside it."""
    from fused_render.ai import registry, supervisor

    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    client, _ = _client(tmp_path, monkeypatch)

    stopped = []
    monkeypatch.setattr(supervisor, "_terminate", lambda worker: stopped.append(worker))
    worker = supervisor.Worker(model="mlx-community/whisper-large-v3-turbo",
                               capability=registry.SPEECH_TO_TEXT,
                               runner_code="mlx-whisper", state="ready")
    supervisor._workers[registry.SPEECH_TO_TEXT] = worker
    try:
        client.put("/api/prefs",
                   json={"engines": {"automatic-speech-recognition": "faster-whisper"}},
                   headers=FUSED)
        assert [w.model for w in stopped] == [worker.model]
        assert registry.SPEECH_TO_TEXT not in supervisor._workers
    finally:
        supervisor.reset()


def test_a_resident_model_the_switch_does_not_affect_is_LEFT_ALONE(
        tmp_path, monkeypatch):
    """Eviction is a reconciliation, not a blanket unload: changing the speech
    engine must not throw away the chat model somebody is mid-conversation
    with."""
    from fused_render.ai import registry, supervisor

    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    client, _ = _client(tmp_path, monkeypatch)

    stopped = []
    monkeypatch.setattr(supervisor, "_terminate", lambda worker: stopped.append(worker))
    text = supervisor.Worker(model="mlx-community/Qwen3-8B-4bit",
                             capability=registry.TEXT_GENERATION,
                             runner_code="mlx-text", state="ready")
    supervisor._workers[registry.TEXT_GENERATION] = text
    try:
        client.put("/api/prefs",
                   json={"engines": {"automatic-speech-recognition": "faster-whisper"}},
                   headers=FUSED)
        assert stopped == []
        assert supervisor._workers[registry.TEXT_GENERATION] is text
    finally:
        supervisor.reset()
