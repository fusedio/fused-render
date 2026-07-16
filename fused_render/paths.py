import os
import tempfile


def state_dir() -> str:
    from fused_render._branch import branch_ref

    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    ref = branch_ref()
    return os.path.join(base, ref) if ref else base


def state_path(*parts: str) -> str:
    return os.path.join(state_dir(), *parts)


def cache_dir() -> str:
    override = os.environ.get("FUSED_RENDER_CACHE_DIR")
    return os.path.expanduser(override) if override else os.path.expanduser("~/.fused-render/cache")


def cache_path(*parts: str) -> str:
    return os.path.join(cache_dir(), *parts)


def daemon_cache_dir(name: str) -> str:
    override = os.environ.get("FUSED_RENDER_CACHE_DIR")
    if override:
        return os.path.join(os.path.expanduser(override), "daemons", name)
    return os.path.expanduser(f"~/.cache/fused-render-{name}")


def binary_dir() -> str:
    if "FUSED_RENDER_HOME" in os.environ:
        return state_path("bin")
    return os.path.expanduser("~/.fused-render/bin")


def runtime_dir(default: str) -> str:
    override = os.environ.get("FUSED_RENDER_RUNTIME_DIR")
    return os.path.expanduser(override) if override else os.path.expanduser(default)


def temp_dir() -> str:
    override = os.environ.get("FUSED_RENDER_TEMP_DIR")
    return os.path.expanduser(override) if override else tempfile.gettempdir()


def desktop_instance() -> tuple[str, str] | None:
    instance_id = os.environ.get("FUSED_RENDER_DESKTOP_INSTANCE_ID")
    token = os.environ.get("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN")
    if bool(instance_id) != bool(token):
        raise RuntimeError("desktop instance id and token must be set together")
    return (instance_id, token) if instance_id and token else None
