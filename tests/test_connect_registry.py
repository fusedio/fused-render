"""connect.registry: every shipped pack validates, bad packs are skipped by
name, `_` keys and `${VAR}` placeholders are handled, env client ids fill in."""
import json
import logging
import os

import pytest

from fused_render.connect import registry

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_PACKS = os.path.join(REPO, "tests", "_fake_provider")
FIRST_WAVE = ("github", "notion", "slack")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("FUSED_RENDER_CONNECT_") or k == "FUSED_RENDER_FAKE_VENDOR_URL":
            monkeypatch.delenv(k)
    registry._warned.clear()


def _builtin_pack_files():
    out = []
    for name in sorted(os.listdir(registry.BUILTIN_DIR)):
        p = os.path.join(registry.BUILTIN_DIR, name, "provider.json")
        if os.path.isfile(p):
            out.append(p)
    return out


@pytest.mark.parametrize("path", _builtin_pack_files(), ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_every_shipped_pack_validates_and_has_an_icon(path):
    with open(path, encoding="utf-8") as f:
        d = registry._expand(registry._strip_private(json.load(f)))
    registry.validate(d, path)
    assert os.path.isfile(os.path.join(os.path.dirname(path), "icon.svg"))


def test_first_wave_is_listed_sorted_with_absolute_dirs():
    keys = [p.key for p in registry.providers()]
    assert keys == sorted(keys)
    for k in FIRST_WAVE:
        assert k in keys
        p = registry.get(k)
        assert os.path.isabs(p.dir) and os.path.isabs(p.icon) and p.icon.endswith("icon.svg")
        assert p.privacy
    assert registry.get("nope") is None


def test_notion_is_mcp_oauth_against_its_hosted_server():
    p = registry.get("notion")
    assert [a["kind"] for a in p.auth] == ["mcp-oauth"]
    assert p.auth[0]["resource"] == "https://mcp.notion.com/mcp"
    assert p.tools == {"remote": "https://mcp.notion.com/mcp", "native": False}


def test_github_auth_order_and_comment_dropped():
    p = registry.get("github")
    assert [a["kind"] for a in p.auth] == ["cli", "oauth-device", "api-key"]
    assert p.auth[0]["command"] == ["gh", "auth", "token"]
    assert p.auth[1]["client_id"] == ""
    assert p.tools["remote"] == "https://api.githubcopilot.com/mcp/"
    with open(os.path.join(p.dir, "provider.json"), encoding="utf-8") as f:
        assert "_comment" in json.load(f)
    assert not any(k.startswith("_") for a in p.auth for k in a)


def test_github_client_id_comes_from_the_environment(monkeypatch):
    p = registry.get("github")
    ok, reason = registry.availability(p, p.auth[1])
    assert not ok and "FUSED_RENDER_CONNECT_GITHUB_CLIENT_ID" in reason
    monkeypatch.setenv("FUSED_RENDER_CONNECT_GITHUB_CLIENT_ID", "Iv1.abc")
    p = registry.get("github")
    assert p.auth[1]["client_id"] == "Iv1.abc"
    assert registry.availability(p, p.auth[1]) == (True, None)
    assert registry.availability(p, p.auth[2]) == (True, None)  # api-key


def test_cli_availability_is_about_the_command_on_path(monkeypatch):
    p = registry.get("github")
    monkeypatch.setattr(registry.shutil, "which", lambda exe: None)
    ok, reason = registry.availability(p, p.auth[0])
    assert not ok and "gh" in reason
    monkeypatch.setattr(registry.shutil, "which", lambda exe: "/usr/bin/" + exe)
    assert registry.availability(p, p.auth[0]) == (True, None)


def test_slack_pack_and_manifest_agree():
    p = registry.get("slack")
    a = p.auth[0]
    assert a["kind"] == "oauth-pkce" and a["client_id"] == ""
    assert a["redirect"] == {"host": "localhost", "ports": [47823, 47824, 47825, 47826, 47827],
                             "path": "/callback"}
    assert p.tools["remote"] is None
    assert p.tools["native"] == os.path.isfile(os.path.join(p.dir, "tools.py"))
    ok, reason = registry.availability(p, a)
    assert not ok and "manifest" in reason
    with open(os.path.join(p.dir, a["byo"]["manifest"]), encoding="utf-8") as f:
        m = json.load(f)
    scopes = a["scopes"]["user_scope"].split(",")
    assert len(scopes) == 13
    assert m["oauth_config"]["scopes"]["user"] == scopes
    assert "bot" not in m["oauth_config"]["scopes"]
    assert m["oauth_config"]["pkce_enabled"] is True
    assert m["oauth_config"]["redirect_urls"] == [
        f"http://localhost:{port}/callback" for port in a["redirect"]["ports"]]
    assert "token_rotation_enabled" not in m.get("settings", {})


def test_slack_client_id_env_override(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_SLACK_CLIENT_ID", "123.456")
    p = registry.get("slack")
    assert p.auth[0]["client_id"] == "123.456"
    assert registry.availability(p, p.auth[0]) == (True, None)


def test_no_secret_in_any_shipped_pack():
    for path in _builtin_pack_files():
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        for a in d["auth"]:
            assert not a.get("client_secret"), path


# ----------------------------------------------------------- validation ---

def _good():
    return {"name": "X", "auth": [{"kind": "api-key", "label": "Key",
                                   "identity_url": "https://x.example/me"}],
            "tools": {"remote": None}, "privacy": "p"}


@pytest.mark.parametrize("mutate, field", [
    (lambda d: d.pop("name"), "name"),
    (lambda d: d.update(auth=[]), "auth"),
    (lambda d: d["auth"][0].update(kind="magic"), "auth[0].kind"),
    (lambda d: d["auth"][0].pop("identity_url"), "auth[0].identity_url"),
    (lambda d: d["auth"][0].update(identity_url="/me"), "auth[0].identity_url"),
    (lambda d: d["auth"][0].update(clientid="x"), "auth[0].clientid"),
    (lambda d: d.update(tools={"remote": "ftp://x"}), "tools.remote"),
    (lambda d: d.update(tools={"native": True}), "tools.native"),
    (lambda d: d.update(colour="red"), "colour"),
    (lambda d: d.update(auth=[{"kind": "oauth-pkce", "client_id": "", "authorize_url": "https://a/x",
                               "token_url": "https://a/t",
                               "redirect": {"host": "localhost", "ports": ["80"], "path": "/cb"}}]),
     "auth[0].redirect.ports"),
    (lambda d: d.update(auth=[{"kind": "oauth-pkce", "client_id": "", "authorize_url": "https://a/x",
                               "token_url": "https://a/t", "scopes": {"scope": ["a"]},
                               "redirect": {"host": "localhost", "ports": None, "path": "/cb"}}]),
     "auth[0].scopes"),
    (lambda d: d.update(auth=[{"kind": "cli", "command": "gh auth token",
                               "identity_url": "https://a/u"}]), "auth[0].command"),
    (lambda d: d.update(auth=[{"kind": "mcp-oauth"}]), "auth[0].resource"),
    (lambda d: d["auth"][0].update(identity_method="PUT"), "auth[0].identity_method"),
])
def test_validate_names_the_file_and_the_field(mutate, field):
    d = _good()
    registry.validate(d, "/x/provider.json")
    mutate(d)
    with pytest.raises(ValueError) as e:
        registry.validate(d, "/x/provider.json")
    assert str(e.value).startswith(f"/x/provider.json: {field}:")


def test_a_bad_pack_is_skipped_with_one_warning(tmp_path, monkeypatch, caplog):
    good = tmp_path / "goodone"
    good.mkdir()
    (good / "provider.json").write_text(json.dumps(_good()))
    bad = tmp_path / "badone"
    bad.mkdir()
    d = _good()
    d["auth"][0]["kind"] = "magic"
    (bad / "provider.json").write_text(json.dumps(d))
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "provider.json").write_text("{not json")
    (tmp_path / "notapack").mkdir()
    monkeypatch.setenv("FUSED_RENDER_CONNECT_PROVIDERS", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="fused_render.connect.registry"):
        keys = [p.key for p in registry.providers()]
        registry.providers()
    assert "goodone" in keys and "badone" not in keys and "broken" not in keys
    assert "github" in keys  # built-ins survive a bad extra pack
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("badone" in m and "auth[0].kind" in m for m in msgs) == 1
    assert sum("broken" in m and "unreadable JSON" in m for m in msgs) == 1


def test_env_entry_may_be_a_pack_itself_and_overrides_a_builtin(tmp_path, monkeypatch):
    pack = tmp_path / "notion"
    pack.mkdir()
    d = _good()
    d["name"] = "Notion (dev)"
    (pack / "provider.json").write_text(json.dumps(d))
    monkeypatch.setenv("FUSED_RENDER_CONNECT_PROVIDERS", str(pack))
    p = registry.get("notion")
    assert p.name == "Notion (dev)" and p.icon is None and p.dir == str(pack)


# ---------------------------------------------------------- placeholders ---

def test_fake_pack_expands_the_vendor_url(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_PROVIDERS", FAKE_PACKS)
    monkeypatch.setenv("FUSED_RENDER_FAKE_VENDOR_URL", "http://127.0.0.1:5555")
    p = registry.get("fakevendor")
    assert [a["kind"] for a in p.auth] == ["mcp-oauth", "oauth-pkce", "oauth-device", "api-key"]
    assert p.auth[0]["resource"] == "http://127.0.0.1:5555/mcp"
    assert p.auth[1]["token_url"] == "http://127.0.0.1:5555/token"
    assert p.auth[1]["redirect"]["ports"] is None
    assert p.tools == {"remote": "http://127.0.0.1:5555/mcp", "native": False}
    assert p.icon and os.path.isfile(p.icon)


def test_fake_pack_without_its_env_var_is_skipped_by_name(monkeypatch, caplog):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_PROVIDERS", FAKE_PACKS)
    with caplog.at_level(logging.WARNING, logger="fused_render.connect.registry"):
        assert registry.get("fakevendor") is None
    assert any("${FUSED_RENDER_FAKE_VENDOR_URL}" in r.getMessage() for r in caplog.records)


def test_placeholders_and_private_keys_at_every_level():
    raw = {"a": "${HOME_X}/y", "_skip": 1, "b": [{"_c": 2, "d": "${NOPE_UNSET_123}"}]}
    os.environ["HOME_X"] = "/h"
    try:
        out = registry._expand(registry._strip_private(raw))
    finally:
        del os.environ["HOME_X"]
    assert out == {"a": "/h/y", "b": [{"d": "${NOPE_UNSET_123}"}]}
