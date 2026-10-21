import os
from typing import Any

from dotenv import load_dotenv
from tavily import (  # type: ignore[import-untyped]
    BadRequestError,
    ForbiddenError,
    InvalidAPIKeyError,
    MissingAPIKeyError,
    TavilyClient,
    TavilyError,
    UsageLimitExceededError,
)
from tavily import (  # type: ignore[import-untyped]
    TimeoutError as TavilyTimeoutError,
)

from coder.tools.base import (
    ToolContext,
    ToolFailure,
    limited,
    observation,
    schema,
    text_arg,
)
from coder.types import ToolResult

load_dotenv()


class web_search:
    name = 'web_search'
    description = 'the tool is used to search relevant information online'
    is_read_only = True

    def schema(self) -> dict[str, Any]:
        return schema(self.name, self.description,
                      properties={"query": {"type": "string"}},required=["query"])

    def execute(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def run():
            api_key = os.getenv("TAVILY_API_KEY", None)
            MAX_WEB_CONTENT_CHAR = int(os.getenv("MAX_WEB_CONTENT_CHAR",600))
            MAX_WEB_NUM = int(os.getenv("MAX_WEB_NUM",5))
            if not api_key:
                raise ToolFailure("TAVILY api无法读取，请检查api")
            query = text_arg(arguments,"query")
            try:
                client = TavilyClient(api_key=api_key)
                response = client.search(query,max_results=MAX_WEB_NUM)
            except MissingAPIKeyError as exc:
                raise ToolFailure("未配置 TAVILY_API_KEY") from exc
            except InvalidAPIKeyError as exc:
                raise ToolFailure("Tavily API key 无效，请检查 TAVILY_API_KEY") from exc
            except UsageLimitExceededError as exc:
                raise ToolFailure("Tavily API 额度已用尽，请检查账户额度") from exc
            except BadRequestError as exc:
                raise ToolFailure("Tavily 搜索请求参数无效") from exc
            except ForbiddenError as exc:
                raise ToolFailure("Tavily 搜索被拒绝，请检查 API 权限") from exc
            except TavilyTimeoutError as exc:
                raise ToolFailure("Tavily 搜索超时，请稍后重试") from exc
            except TavilyError as exc:
                raise ToolFailure("Tavily 搜索请求失败，请检查网络或服务状态") from exc
            results = response.get("results")
            content = []
            for index, result in enumerate(results):
                this_content =  result.get("content") if len(result.get("content")) <= MAX_WEB_CONTENT_CHAR else result.get("content")[:MAX_WEB_CONTENT_CHAR]
                content.append(f"index: {index+1}\ntitle:{result.get('title')}\ncontent:{this_content}")
            return limited('\n'.join(content),ctx.settings.max_tool_output_chars)
        return observation('',self.name,run)




