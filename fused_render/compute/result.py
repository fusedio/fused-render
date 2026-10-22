# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see NOTICE.
"""What one call returns: the runner's wire result, parsed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResponseMeta:
    """Content-type metadata for a served response (from a `fused.Response`).

    Carried out-of-band from `return_value` so user data can never spoof it.
    `body_encoding == "base64"` means `return_value` holds base64-encoded bytes
    (used for binary bodies, which cannot otherwise cross the JSON boundary).
    """

    media_type: str
    status_code: int = 200
    headers: dict[str, str] | None = None
    body_encoding: str | None = None

    @classmethod
    def from_wire(cls, d: dict | None) -> ResponseMeta | None:
        """Build from the runner's `response` dict (the JSON wire form), or None."""
        if not d:
            return None
        return cls(
            media_type=d.get("media_type", "application/json"),
            status_code=d.get("status_code", 200),
            headers=d.get("headers") or None,
            body_encoding=d.get("body_encoding"),
        )

    def to_wire(self) -> dict:
        """JSON-serializable form, for a child call's result file."""
        return {
            "media_type": self.media_type,
            "status_code": self.status_code,
            "headers": self.headers,
            "body_encoding": self.body_encoding,
        }


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    return_value: Any
    duration_ms: int
    # Set when the code returned a fused.Response; None means the return_value is JSON.
    response: ResponseMeta | None = None
    # Set whenever the run did not succeed: the handler's `error` field (which may
    # be set even when return_value is present) or a failure with no handler
    # response at all (timeout, crash, missing or unparseable output). `None`
    # therefore reliably means a successful run.
    error: str | None = None
    # Notes the handler attached to a successful run.
    warnings: list[str] | None = None
