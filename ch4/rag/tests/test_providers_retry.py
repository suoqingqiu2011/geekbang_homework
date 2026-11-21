"""重试策略：只重试瞬时故障，鉴权/参数错误立即失败。"""

from __future__ import annotations

import pytest

from rag_core import providers


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def _always_raise(exc: Exception, calls: list[int]):
    def run() -> None:
        calls.append(1)
        raise exc

    return run


def test_client_error_is_not_retried() -> None:
    calls: list[int] = []
    with pytest.raises(_HttpError):
        providers._retry(_always_raise(_HttpError(401), calls), attempts=3, base_delay=0.0)
    assert len(calls) == 1


def test_server_error_is_retried_up_to_attempts() -> None:
    calls: list[int] = []
    with pytest.raises(_HttpError):
        providers._retry(_always_raise(_HttpError(500), calls), attempts=3, base_delay=0.0)
    assert len(calls) == 3


def test_rate_limit_is_retried() -> None:
    calls: list[int] = []
    with pytest.raises(_HttpError):
        providers._retry(_always_raise(_HttpError(429), calls), attempts=2, base_delay=0.0)
    assert len(calls) == 2


def test_unknown_error_is_not_retried() -> None:
    calls: list[int] = []
    with pytest.raises(RuntimeError):
        providers._retry(_always_raise(RuntimeError("boom"), calls), attempts=3, base_delay=0.0)
    assert len(calls) == 1


def test_success_returns_without_retry() -> None:
    calls: list[int] = []

    def run() -> int:
        calls.append(1)
        return 42

    assert providers._retry(run, attempts=3, base_delay=0.0) == 42
    assert len(calls) == 1


def test_openai_compatible_metadata_for_unknown_model() -> None:
    llm = providers.OpenAICompatible(
        model="deepseek-chat",
        api_key="dummy",
        api_base="https://example.invalid/v1",
    )

    assert llm.metadata.is_chat_model is True
    assert llm.metadata.model_name == "deepseek-chat"


def test_brief_error_redacts_key() -> None:
    detail = providers._brief_error(Exception("bad key sk-abcdefgh12345678"))

    assert "sk-abcdefgh12345678" not in detail
    assert "[REDACTED]" in detail
