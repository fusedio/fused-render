"""Give fused's HTTP calls a read timeout a real deployment can meet.

The fused SDK's `Options.request_timeout` defaults to 5 seconds and governs
EVERY workbench API call, including `import-toml` — the POST that uploads a
whole canvas zip and has the server replace the collection's UDF set. The SDK
only raises it (to 25) in `fused._env()` for `dev` and PR environments; prod,
unstable and a custom deployment loaded from `~/.fused/settings.toml` keep 5.
An on-prem deployment answers that call slower than that, so every push there
died with `ReadTimeoutError(... read timeout=5)` (owner, 2026-10-27).

Called by every child script of ours that imports fused, AFTER its
`fused._env()` block: `fused.options` is the one object `fused.workbench.api`
aliases as OPTIONS, so an attribute assignment here is what the API sees. A
value the user set themselves (`request_timeout = ...` in settings.toml, or
the SDK's own dev/PR bump) is in `model_fields_set` and is left alone.
"""

# (connect, read): connecting stays quick so a dead host still fails fast;
# only the wait for the server's answer grows. 60 s matches the SDK's own
# `metadata_request_timeout` for its slow endpoints.
REQUEST_TIMEOUT = (5.0, 60.0)


def raise_request_timeout() -> None:
    import fused

    options = fused.options
    if "request_timeout" in options.model_fields_set:
        return
    options.request_timeout = REQUEST_TIMEOUT
