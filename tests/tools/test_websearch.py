"""Web 搜索的服务端异常应成为可解释的工具观察。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from tavily import (
    BadRequestError,
    ForbiddenError,
    InvalidAPIKeyError,
    MissingAPIKeyError,
    TavilyError,
    UsageLimitExceededError,
)
from tavily import TimeoutError as TavilyTimeoutError

from coder.config import Settings
from coder.tools.base import ToolContext
from coder.tools.websearch import web_search


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (MissingAPIKeyError("provider detail"), "未配置 TAVILY_API_KEY"),
        (InvalidAPIKeyError("provider detail"), "API key 无效"),
        (UsageLimitExceededError("provider detail"), "额度已用尽"),
        (BadRequestError("provider detail"), "请求参数无效"),
        (ForbiddenError("provider detail"), "搜索被拒绝"),
        (TavilyTimeoutError("provider detail"), "搜索超时"),
        (TavilyError("provider detail"), "请求失败"),
    ],
)
def test_web_search_classifies_provider_errors(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, failure: TavilyError, expected: str,
) -> None:
    """使用假客户端验证分类与错误脱敏，不发送真实网络请求。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    class FailingClient:
        def __init__(self, api_key: str) -> None:
            assert api_key == "test-key"

        def search(self, query: str, max_results: int) -> dict[str, object]:
            assert query == "sample query"
            assert max_results == 5
            raise failure

    websearch_module = importlib.import_module("coder.tools.websearch")
    monkeypatch.setattr(websearch_module, "TavilyClient", FailingClient)
    settings = Settings("scripted", None, None, workspace)
    context = ToolContext(workspace, workspace, settings)

    result = web_search().execute({"query": "sample query"}, context)

    assert result.is_error
    assert expected in result.content
    assert "provider detail" not in result.content
    assert "Internal tool error" not in result.content
