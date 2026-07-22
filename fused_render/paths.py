import os


def desktop_instance() -> tuple[str, str] | None:
    instance_id = os.environ.get("FUSED_RENDER_DESKTOP_INSTANCE_ID")
    token = os.environ.get("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN")
    if bool(instance_id) != bool(token):
        raise RuntimeError("desktop instance id and token must be set together")
    return (instance_id, token) if instance_id and token else None
