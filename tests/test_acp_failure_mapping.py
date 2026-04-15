from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentao.acp_client import AcpClientError, AcpErrorCode, AcpRpcError
from agentao.acp_client.client import AcpInteractionRequiredError

from kanban.agent_profiles import load_default_config
from kanban.executors.acp_failure import AcpFailureKind, classify
from kanban.executors.backends.base import BackendRequest, BackendResponse
from kanban.executors.multi_backend import MultiBackendExecutor
from kanban.models import AgentRole, Card, CardStatus


REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "kanban" / "defaults"


# ---------- classify() unit tests ----------


@pytest.mark.parametrize("code", [AcpErrorCode.CONFIG_INVALID, AcpErrorCode.SERVER_NOT_FOUND])
def test_classify_config_codes(code: AcpErrorCode) -> None:
    exc = AcpClientError("x", code=code)
    assert classify(exc) == AcpFailureKind.CONFIG


@pytest.mark.parametrize("code", [
    AcpErrorCode.PROCESS_START_FAIL,
    AcpErrorCode.HANDSHAKE_FAIL,
    AcpErrorCode.REQUEST_TIMEOUT,
    AcpErrorCode.TRANSPORT_DISCONNECT,
    AcpErrorCode.PROTOCOL_ERROR,
    AcpErrorCode.SERVER_BUSY,
])
def test_classify_infrastructure_codes(code: AcpErrorCode) -> None:
    exc = AcpClientError("x", code=code)
    assert classify(exc) == AcpFailureKind.INFRASTRUCTURE


def test_classify_interaction_required() -> None:
    exc = AcpInteractionRequiredError(server="s", method="session/request_permission")
    assert classify(exc) == AcpFailureKind.INTERACTION_REQUIRED


def test_classify_rpc_error_maps_to_infrastructure() -> None:
    # AcpRpcError shadows `code` with an int but exposes `acp_code` enum.
    exc = AcpRpcError(rpc_code=-32603, rpc_message="internal error")
    assert classify(exc) == AcpFailureKind.INFRASTRUCTURE


def test_classify_unknown_code_defaults_to_infrastructure() -> None:
    class _Bogus:
        code = "something_new"
    assert classify(_Bogus()) == AcpFailureKind.INFRASTRUCTURE


# ---------- executor wiring ----------


@dataclass
class _RaisingBackend:
    backend_type: str
    exc: Exception
    calls: int = 0

    def invoke(self, request: BackendRequest) -> BackendResponse:
        self.calls += 1
        raise self.exc


@dataclass
class _ReturningBackend:
    backend_type: str
    raw: str
    calls: int = 0

    def invoke(self, request: BackendRequest) -> BackendResponse:
        self.calls += 1
        return BackendResponse(
            raw_text=self.raw,
            prompt_version="v1",
            spec_name=request.profile.name,
        )


_OK_RAW = 'done\n```json\n{"ok": true, "summary": "ok", "output": "code"}\n```\n'


def _exec_with(acp_backend) -> MultiBackendExecutor:
    return MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": _ReturningBackend("subagent", _OK_RAW), "acp": acp_backend},
    )


def _card_using_acp() -> Card:
    return Card(
        title="t",
        goal="g",
        status=CardStatus.DOING,
        agent_profile="gemini-worker",
        agent_profile_source="manual",
    )


def test_config_failure_blocks_card_without_retry() -> None:
    acp = _RaisingBackend(
        "acp",
        AcpClientError("missing", code=AcpErrorCode.SERVER_NOT_FOUND),
    )
    result = _exec_with(acp).run(AgentRole.WORKER, _card_using_acp())
    assert result.next_status == CardStatus.BLOCKED
    assert "server_not_found" in result.updates["blocked_reason"]
    assert acp.calls == 1  # not retried, not fallen back


def test_interaction_required_blocks_card_without_fallback() -> None:
    acp = _RaisingBackend(
        "acp",
        AcpInteractionRequiredError(server="gemini-worker", method="permission"),
    )
    subagent_sentinel = _ReturningBackend("subagent", _OK_RAW)
    executor = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": subagent_sentinel, "acp": acp},
    )
    result = executor.run(AgentRole.WORKER, _card_using_acp())
    assert result.next_status == CardStatus.BLOCKED
    assert "requires user input" in result.updates["blocked_reason"]
    # Explicit: no fallback to the subagent profile when interaction is required.
    assert subagent_sentinel.calls == 0


def test_infrastructure_failure_without_fallback_raises() -> None:
    acp = _RaisingBackend(
        "acp",
        AcpClientError("boom", code=AcpErrorCode.REQUEST_TIMEOUT),
    )
    # Use a profile with NO fallback — default-planner-like config wouldn't do,
    # so build a card using gemini-worker but strip its fallback first.
    cfg = load_default_config()
    from dataclasses import replace
    cfg.profiles["gemini-worker"] = replace(
        cfg.profiles["gemini-worker"], fallback=None
    )
    executor = MultiBackendExecutor(
        config=cfg,
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": _ReturningBackend("subagent", _OK_RAW), "acp": acp},
    )
    with pytest.raises(RuntimeError, match="backend acp call failed"):
        executor.run(AgentRole.WORKER, _card_using_acp())


def test_infrastructure_failure_falls_back_once() -> None:
    acp = _RaisingBackend(
        "acp",
        AcpClientError("transport dropped", code=AcpErrorCode.TRANSPORT_DISCONNECT),
    )
    sub = _ReturningBackend("subagent", _OK_RAW)
    executor = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": sub, "acp": acp},
    )
    result = executor.run(AgentRole.WORKER, _card_using_acp())
    assert result.next_status == CardStatus.REVIEW
    assert result.updates["outputs"]["implementation"] == "code"
    assert acp.calls == 1
    assert sub.calls == 1  # fallback invoked exactly once


def test_fallback_also_fails_reraises_infrastructure() -> None:
    acp = _RaisingBackend(
        "acp",
        AcpClientError("t1", code=AcpErrorCode.REQUEST_TIMEOUT),
    )
    # Replace the default-worker subagent with one that also fails.
    sub = _RaisingBackend("subagent", RuntimeError("sub broke"))
    executor = MultiBackendExecutor(
        config=load_default_config(),
        agents_dir=REPO_AGENTS_DIR,
        backends={"subagent": sub, "acp": acp},
    )
    with pytest.raises(RuntimeError, match="backend subagent call failed"):
        executor.run(AgentRole.WORKER, _card_using_acp())
    assert acp.calls == 1
    assert sub.calls == 1
