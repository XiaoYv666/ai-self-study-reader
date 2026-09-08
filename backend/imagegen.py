"""P2：画图题真出图（image_gen，OpenAI-compatible image-model）。

- 走 providers.get_provider("image_gen") 的页面/数据库配置
- OpenAI images API 兼容：POST {base_url}/images/generations，body {model, prompt, size}
- 响应 data[0].b64_json（优先）或 url（兜底再下载）
- 认证：Bearer key + extra_headers（OpenAI-compatible 网关 CF Access 门禁，与 vision 同套凭据）
- httpx2 AsyncClient(trust_env=False)：不读系统/环境代理（macOS scutil 代理可能指向死端口）
- 出图慢（15-40s）：timeout 120s；失败抛 RuntimeError 带原因，调用方转 image 事件的 error 字段
"""
import base64
import binascii
import logging

import httpx2 as httpx

from providers import get_provider

logger = logging.getLogger(__name__)

TIMEOUT_S = 120.0  # image-model 实测 15-40s，留足余量


def build_request_payload(prompt: str, size: str) -> dict:
    """请求体拼装（独立成函数便于单测）。model 从 provider 配置读，不写死。"""
    cfg = get_provider("image_gen")
    return {"model": cfg.model, "prompt": prompt, "size": size, "n": 1}


def extract_image_bytes(data: dict) -> bytes | None:
    """从 images API 响应的 data[0] 提取图片字节：b64_json 优先，url 由调用方另行下载。"""
    first = (data or {}).get("data") or []
    if not first or not isinstance(first[0], dict):
        return None
    b64 = first[0].get("b64_json")
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except (binascii.Error, ValueError):
        return None


async def generate_image(prompt: str, size: str = "1024x1024") -> bytes:
    """生成一张图，返回 PNG/JPEG 字节。失败抛 RuntimeError（带原因），由调用方降级。"""
    cfg = get_provider("image_gen")
    prompt = (prompt or "").strip()
    if not prompt:
        raise RuntimeError("图片 prompt 为空")

    url = f"{cfg.base_url}/images/generations"
    headers = {"Authorization": f"Bearer {cfg.api_key}", **cfg.extra_headers}
    payload = build_request_payload(prompt, size)

    async with httpx.AsyncClient(trust_env=False, timeout=TIMEOUT_S) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"image_gen HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            result = resp.json()
        except Exception as e:  # noqa: BLE001 —— 网关异常载荷统一转 RuntimeError
            raise RuntimeError(f"image_gen 响应非 JSON: {e}") from e

        img = extract_image_bytes(result)
        if img:
            return img

        # 兜底：data[0].url → 再拉一次字节
        first = (result.get("data") or [{}])[0] if isinstance(result, dict) else {}
        img_url = first.get("url") if isinstance(first, dict) else None
        if img_url:
            dl = await client.get(img_url)
            if dl.status_code == 200 and dl.content:
                return dl.content
            raise RuntimeError(f"image_gen 下载图片失败 HTTP {dl.status_code}")

    raise RuntimeError("image_gen 响应无 b64_json/url")
