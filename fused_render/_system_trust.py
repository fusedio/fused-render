"""Make Python's TLS clients trust the OS certificate store.

Behind a TLS-intercepting corporate proxy (Zscaler and friends) the proxy's
root CA lives in the OS trust store but not in certifi, so `requests` (the
bundled `fused` package's OAuth token exchange) and httpx fail verification
and sign-in loops. `truststore.inject_into_ssl()` points every
`ssl.SSLContext` in the process at the OS store instead.

The macOS app needs one more step: py2app's generated `__boot__.py`
unconditionally sets SSL_CERT_FILE / SSL_CERT_DIR to a stale bundled
`openssl.ca`, which httpx honours and child processes inherit. Only values
pointing inside the bundle (RESOURCEPATH) are dropped; a user's own override
elsewhere keeps working.

Called from `fused_render/__init__.py`, so every `python -m fused_render.<x>`
process gets it. Set FUSED_RENDER_SYSTEM_TRUST=0 to opt out.
"""
import logging
import os

logger = logging.getLogger("fused_render._system_trust")

_done = False


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _strip_py2app_vars() -> None:
    root = os.environ.get("RESOURCEPATH")
    if not root:
        return
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = os.environ.get(var)
        if value and _inside(value, root):
            os.environ.pop(var, None)


def use_system_trust() -> None:
    global _done
    if _done:
        return
    if os.environ.get("FUSED_RENDER_SYSTEM_TRUST", "").strip().lower() in (
            "0", "false", "no", "off"):
        return
    _done = True
    try:
        _strip_py2app_vars()
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        logger.debug("truststore not installed; using the default trust store")
    except Exception as e:  # noqa: BLE001
        logger.warning("system trust store unavailable: %s", e)
