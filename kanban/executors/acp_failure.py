"""Map `agentao.acp_client.AcpErrorCode` to kanban failure semantics.

Three kanban-facing categories (Phase 5):

- ``CONFIG``: routing / configuration failure. Terminal — the card is moved
  to BLOCKED and the retry matrix does not fire. No fallback either,
  because the same misconfiguration will repeat.
- ``INFRASTRUCTURE``: the backend plumbing broke (process didn't start,
  RPC timed out, transport dropped, ...). Retryable; also triggers a
  single fallback attempt if the profile declares one.
- ``INTERACTION_REQUIRED``: the server asked for user input during a
  non-interactive turn. Not retryable and not a fallback candidate —
  re-running or swapping backends won't supply the missing input.

Classification is done strictly via ``AcpClientError.code`` so string
matching on messages is unnecessary.
"""

from __future__ import annotations

from enum import StrEnum


class AcpFailureKind(StrEnum):
    CONFIG = "config"
    INFRASTRUCTURE = "infrastructure"
    INTERACTION_REQUIRED = "interaction_required"


_CONFIG_CODES = frozenset({"config_invalid", "server_not_found"})
_INFRA_CODES = frozenset({
    "process_start_fail",
    "handshake_fail",
    "request_timeout",
    "transport_disconnect",
    "protocol_error",
    "server_busy",
})


def classify(error: "AcpClientErrorLike") -> AcpFailureKind:
    """Classify an ACP error by its structured ``code``.

    Accepts any object exposing a ``code`` attribute whose string value
    matches the ``AcpErrorCode`` vocabulary. ``AcpRpcError`` is treated
    as ``INFRASTRUCTURE`` (JSON-RPC error == protocol-level failure);
    it shadows ``code`` with an int, so we fall through on ``acp_code``.
    """
    code_value = _code_string(error)
    if code_value == "interaction_required":
        return AcpFailureKind.INTERACTION_REQUIRED
    if code_value in _CONFIG_CODES:
        return AcpFailureKind.CONFIG
    if code_value in _INFRA_CODES:
        return AcpFailureKind.INFRASTRUCTURE
    # Unknown codes: treat as infrastructure so the retry matrix is the
    # default escape hatch rather than an immediate BLOCKED.
    return AcpFailureKind.INFRASTRUCTURE


def _code_string(error: "AcpClientErrorLike") -> str:
    # `AcpRpcError` shadows `code` with a raw JSON-RPC int but exposes
    # the enum via `acp_code`. Prefer the enum when present.
    acp_code = getattr(error, "acp_code", None)
    if acp_code is not None:
        return getattr(acp_code, "value", str(acp_code))
    code = getattr(error, "code", None)
    return getattr(code, "value", str(code) if code is not None else "")


class AcpClientErrorLike:
    """Structural type hint — any object with ``.code`` is accepted."""

    code: object
