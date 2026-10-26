"""use_system_trust(): the OS trust store for Python TLS, minus py2app's pin."""
import os
import socket
import ssl
import subprocess
import sys
import threading

import pytest
import truststore

from fused_render import _system_trust


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Every test starts un-injected with a scrubbed env, and ends the same way."""
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "RESOURCEPATH",
                "FUSED_RENDER_SYSTEM_TRUST"):
        monkeypatch.delenv(var, raising=False)
    truststore.extract_from_ssl()
    monkeypatch.setattr(_system_trust, "_done", False)
    yield
    truststore.extract_from_ssl()


def test_strips_py2app_vars_inside_resourcepath(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOURCEPATH", str(tmp_path))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "openssl.ca" / "cert.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "openssl.ca" / "no-such-file"))
    _system_trust.use_system_trust()
    assert "SSL_CERT_FILE" not in os.environ
    assert "SSL_CERT_DIR" not in os.environ


def test_keeps_vars_outside_resourcepath(tmp_path, monkeypatch):
    res = tmp_path / "res"
    res.mkdir()
    monkeypatch.setenv("RESOURCEPATH", str(res))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "corp.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "certs"))
    _system_trust.use_system_trust()
    assert os.environ["SSL_CERT_FILE"] == str(tmp_path / "corp.pem")
    assert os.environ["SSL_CERT_DIR"] == str(tmp_path / "certs")


def test_keeps_vars_without_resourcepath(tmp_path, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "openssl.ca" / "cert.pem"))
    _system_trust.use_system_trust()
    assert "SSL_CERT_FILE" in os.environ


def test_sibling_prefix_is_not_inside_resourcepath(tmp_path, monkeypatch):
    res = tmp_path / "res"
    res.mkdir()
    monkeypatch.setenv("RESOURCEPATH", str(res))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "res-other" / "cert.pem"))
    _system_trust.use_system_trust()
    assert "SSL_CERT_FILE" in os.environ


@pytest.mark.parametrize("value", ["0", "false", "No", "OFF"])
def test_opt_out_changes_nothing(value, tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_SYSTEM_TRUST", value)
    monkeypatch.setenv("RESOURCEPATH", str(tmp_path))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "cert.pem"))
    before = ssl.SSLContext
    _system_trust.use_system_trust()
    assert os.environ["SSL_CERT_FILE"] == str(tmp_path / "cert.pem")
    assert ssl.SSLContext is before
    assert ssl.SSLContext is not truststore.SSLContext


def test_injects_and_is_idempotent():
    _system_trust.use_system_trust()
    assert ssl.SSLContext is truststore.SSLContext
    _system_trust.use_system_trust()
    assert ssl.SSLContext is truststore.SSLContext


def test_inject_failure_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(truststore, "inject_into_ssl", boom)
    _system_trust.use_system_trust()


def _run(code, **env):
    full = {k: v for k, v in os.environ.items()
            if k not in ("SSL_CERT_FILE", "SSL_CERT_DIR", "RESOURCEPATH",
                         "FUSED_RENDER_SYSTEM_TRUST")}
    full.update(env)
    out = subprocess.run([sys.executable, "-c", code], env=full,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_import_injects_in_a_fresh_process():
    code = "import fused_render, ssl, truststore; print(ssl.SSLContext is truststore.SSLContext)"
    assert _run(code) == "True"
    assert _run(code, FUSED_RENDER_SYSTEM_TRUST="0") == "False"


def test_import_strips_py2app_var_in_a_fresh_process(tmp_path):
    code = "import fused_render, os; print('SSL_CERT_FILE' in os.environ)"
    out = _run(code, RESOURCEPATH=str(tmp_path),
               SSL_CERT_FILE=str(tmp_path / "openssl.ca" / "cert.pem"))
    assert out == "False"


def test_lan_style_tls_server_handshake_still_works(tmp_path, monkeypatch):
    """After injection `ssl.SSLContext` is truststore's client-side class, whose
    server handshake fails on macOS; lan.py therefore builds its listener's
    context from the stdlib class (lan_tls.server_ssl_context). A real
    handshake against a lan_tls-issued cert proves it."""
    from fused_render import lan_tls

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    lan_tls._ca_by_dir.clear()
    cert, key = lan_tls.ensure_server_cert(["localhost"], ["127.0.0.1"])
    ca = lan_tls.ca_pem_path()

    _system_trust.use_system_trust()
    assert ssl.SSLContext is truststore.SSLContext
    server_ctx = lan_tls.server_ssl_context(cert, key)

    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    port = lsock.getsockname()[1]
    result = {}

    def serve():
        conn, _ = lsock.accept()
        try:
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                result["got"] = tls.recv(4)
                tls.sendall(b"pong")
        except Exception as e:  # noqa: BLE001
            result["err"] = e

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    client_ctx = ssl.create_default_context(cafile=ca)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with client_ctx.wrap_socket(raw, server_hostname="localhost") as tls:
            tls.sendall(b"ping")
            reply = tls.recv(4)
    t.join(10)
    lsock.close()
    assert result.get("got") == b"ping", result
    assert reply == b"pong"
