"""chat 模型调用 + 流式封装（OpenAI 兼容，openai python sdk）。

- 走 providers.get_provider("chat") 的页面/数据库配置
- stream_chat() 产出增量 delta 文本片段，由 main.py 包成 SSE
- M2 扩展位：vision 读图、image_gen 出图都在 providers.py 注册后经此处相似的封装调用
"""
from typing import AsyncIterator, Optional

import httpx2 as httpx  # openai 3.x 传递依赖是 httpx2（httpx 新代包），trust_env 参数同
from openai import AsyncOpenAI

from providers import ProviderConfig, get_provider

_clients: dict[tuple, AsyncOpenAI] = {}


def _get_client(cfg: ProviderConfig) -> AsyncOpenAI:
    key = (cfg.provider_id, cfg.base_url, cfg.api_key, tuple(sorted(cfg.extra_headers.items())))
    if key not in _clients:
        _clients[key] = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            default_headers=cfg.extra_headers or None,
            timeout=300.0,
            max_retries=1,
            # trust_env=False：不读系统/环境代理（macOS scutil 代理可能指向死端口）
            http_client=httpx.AsyncClient(trust_env=False, timeout=300.0),
        )
    return _clients[key]


async def stream_chat(
    messages: list[dict],
    temperature: float = 0.3,
    usage_out: Optional[dict] = None,
    provider_config: Optional[ProviderConfig] = None,
) -> AsyncIterator[str]:
    """流式对话：逐片 yield 正文增量。

    messages: [{"role": "system"|"user"|"assistant", "content": "..."}, ...]
    usage_out: 可选 dict，流结束后被填入 {"prompt_tokens":..,"completion_tokens":..,"total_tokens":..}
    异常向上抛，由调用方（SSE 端点）转成 error 事件。
    """
    cfg = provider_config or get_provider("chat")
    client = _get_client(cfg)
    stream = await client.chat.completions.create(
        model=cfg.model,
        messages=messages,
        temperature=cfg.params.get("temperature", temperature),
        stream=True,
        stream_options={"include_usage": True},  # done 事件携带 usage
    )
    async for chunk in stream:
        usage = getattr(chunk, "usage", None)
        if usage and usage_out is not None:
            usage_out.update(
                {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                }
            )
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        content = getattr(delta, "content", None)
        if content:
            yield content


async def chat_complete(
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 1500,
) -> str:
    """非流式对话：一次返回完整回复文本。

    用于绘图指令 → 结构化 JSON 转换等一次性调用（P2.1 plotgen）。
    复用同一客户端与 provider 配置（模型名不写死）。
    """
    cfg = get_provider("chat")
    client = _get_client(cfg)
    resp = await client.chat.completions.create(
        model=cfg.model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )
    if not resp.choices:
        return ""
    return resp.choices[0].message.content or ""
