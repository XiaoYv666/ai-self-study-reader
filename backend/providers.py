"""多模型路由：运行时仅使用页面写入 SQLite 的模型配置。

旧版 .env 只在模型表完全为空时作为一次性迁移输入；迁移后所有调用、
健康检查和页面展示都以 model_providers/model_configs 为唯一配置源。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from dotenv import dotenv_values

import db

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
_env = dotenv_values(ENV_PATH)

CAPABILITY_LABELS = {"chat": "推理", "vision": "识图", "image_gen": "生图"}
MIGRATION_META_KEY = "legacy_env_models_migrated_v1"


class ModelConfigurationError(RuntimeError):
    """某能力没有可用的页面/数据库配置。"""

    def __init__(self, capability: str):
        self.capability = capability
        label = CAPABILITY_LABELS.get(capability, capability)
        super().__init__(f"{label}模型未配置：请在设置→模型与 API 中配置并启用模型")


def _cfg(key: str, default: str = "") -> str:
    """仅供旧配置迁移读取：环境变量优先，其次项目根 .env。"""
    return os.environ.get(key) or _env.get(key) or default


def _load_json(raw: str | None, fallback):
    try:
        val = json.loads(raw or "")
        return val if isinstance(val, type(fallback)) else fallback
    except Exception:
        return fallback


@dataclass
class ProviderConfig:
    role: str
    base_url: str
    api_key: str
    model: str
    extra_headers: dict = field(default_factory=dict)
    provider_id: int | None = None
    model_config_id: int | None = None
    params: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


def _load_role(role: str, prefix: str) -> ProviderConfig:
    """兼容旧测试/迁移工具；运行时 get_provider 不使用这里的值。"""
    return ProviderConfig(
        role=role,
        base_url=_cfg(f"{prefix}_BASE_URL").rstrip("/"),
        api_key=_cfg(f"{prefix}_API_KEY"),
        model=_cfg(f"{prefix}_MODEL"),
        extra_headers=_load_extra_headers(f"{prefix}_EXTRA_HEADERS"),
    )


def _load_extra_headers(key: str) -> dict:
    raw = _cfg(key)
    val = _load_json(raw, {}) if raw else {}
    return val if isinstance(val, dict) else {}


_ROLES: dict[str, ProviderConfig] = {
    "chat": _load_role("chat", "CHAT"),
    "vision": _load_role("vision", "VISION"),
    "image_gen": _load_role("image_gen", "IMAGE_GEN"),
}


def _provider_from_row(role: str, row) -> ProviderConfig:
    return ProviderConfig(
        role=role,
        base_url=(row["base_url"] or "").rstrip("/"),
        api_key=row["api_key_ciphertext"] or "",
        model=row["model_name"] or "",
        extra_headers=_load_json(row["extra_headers_json"], {}),
        provider_id=int(row["provider_id"]),
        model_config_id=int(row["model_config_id"]),
        params=_load_json(row["params_json"], {}),
    )


def _db_provider(role: str) -> ProviderConfig | None:
    """取 DB 中某能力的最高优先级启用模型。"""
    try:
        conn = db.get_conn()
        row = conn.execute(
            """SELECT mc.id AS model_config_id, mc.model_name, mc.params_json,
                      mp.id AS provider_id, mp.base_url, mp.api_key_ciphertext, mp.extra_headers_json
               FROM model_configs mc
               JOIN model_providers mp ON mp.id = mc.provider_id
               WHERE mc.capability=? AND mc.enabled=1 AND mp.enabled=1
               ORDER BY mc.priority ASC, mc.id ASC
               LIMIT 1""",
            (role,),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return _provider_from_row(role, row)


def get_provider_by_model_config_id(model_config_id: int, role: str = "chat") -> ProviderConfig:
    """按配置 ID 精确取模型；无效状态明确拒绝，不回退默认模型。"""
    if role not in CAPABILITY_LABELS:
        raise KeyError(f"unknown provider role: {role}")
    row = db.get_conn().execute(
        """SELECT mc.id AS model_config_id, mc.model_name, mc.capability,
                  mc.enabled AS model_enabled, mc.params_json,
                  mp.id AS provider_id, mp.name AS provider_name, mp.enabled AS provider_enabled,
                  mp.base_url, mp.api_key_ciphertext, mp.extra_headers_json
           FROM model_configs mc JOIN model_providers mp ON mp.id = mc.provider_id
           WHERE mc.id=?""",
        (model_config_id,),
    ).fetchone()
    if not row:
        raise LookupError(f"模型配置 {model_config_id} 不存在，请刷新模型列表后重试")
    if row["capability"] != role:
        raise ValueError(f"模型配置 {model_config_id} 不是推理模型，请选择 capability='chat' 的模型")
    if not bool(row["model_enabled"]):
        raise ValueError(f"模型 {row['model_name']} 已禁用，请在设置 → 模型与 API 中启用或选择其他模型")
    if not bool(row["provider_enabled"]):
        raise ValueError(f"Provider {row['provider_name']} 已禁用，请在设置 → 模型与 API 中启用或选择其他模型")
    configured = _provider_from_row(role, row)
    if not configured.ready:
        raise ValueError(f"模型 {row['model_name']} 的 Provider 配置不完整，请检查 Base URL、API Key 和模型名")
    return configured


def get_provider(role: str = "chat") -> ProviderConfig:
    """取某能力的页面配置；缺失时给出可操作错误，绝不读取 .env。"""
    if role not in CAPABILITY_LABELS:
        raise KeyError(f"unknown provider role: {role}")
    configured = _db_provider(role)
    if configured is None or not configured.ready:
        raise ModelConfigurationError(role)
    return configured


def describe() -> dict:
    """启动/健康检查：只报告 DB 配置状态，所有秘密均打码。"""
    out = {}
    for role in CAPABILITY_LABELS:
        try:
            p = get_provider(role)
            out[role] = {
                "model": p.model,
                "base_url": p.base_url,
                "ready": True,
                "source": "db",
                "api_key": mask_key(p.api_key),
            }
        except ModelConfigurationError as exc:
            out[role] = {
                "model": None,
                "base_url": None,
                "ready": False,
                "source": "db",
                "api_key": None,
                "message": str(exc),
            }
    return out


def mask_key(api_key: str | None) -> str | None:
    """前端只展示掩码：前 4 位 + **** + 后 4 位。"""
    if not api_key:
        return None
    if len(api_key) <= 8:
        return f"{api_key[:2]}****{api_key[-2:]}"
    return f"{api_key[:4]}****{api_key[-4:]}"


def is_sensitive_header(name: str) -> bool:
    key = name.lower().replace("_", "-")
    return any(token in key for token in (
        "authorization", "api-key", "apikey", "secret", "token", "password", "credential",
    ))


def mask_headers(headers: dict | None) -> dict:
    return {
        str(key): (mask_key(str(value)) if is_sensitive_header(str(key)) else value)
        for key, value in (headers or {}).items()
    }


def merge_masked_headers(stored: dict | None, submitted: dict | None) -> dict:
    """编辑回传掩码值时保留旧 secret；空敏感值也不清除旧值。"""
    old = dict(stored or {})
    merged = dict(submitted or {})
    for key, old_value in old.items():
        new_value = merged.get(key)
        if is_sensitive_header(key) and (new_value in (None, "") or "****" in str(new_value)):
            merged[key] = old_value
    return merged


def _legacy_role(env_values: dict, role: str, prefix: str) -> ProviderConfig:
    def value(suffix: str) -> str:
        return str(env_values.get(f"{prefix}_{suffix}") or "")

    raw_headers = value("EXTRA_HEADERS")
    return ProviderConfig(
        role=role,
        base_url=value("BASE_URL").rstrip("/"),
        api_key=value("API_KEY"),
        model=value("MODEL"),
        extra_headers=_load_json(raw_headers, {}) if raw_headers else {},
    )


def migrate_legacy_env_config(env_values: dict | None = None) -> dict:
    """仅在两张模型表完全为空时，将旧 .env 三类模型一次性导入 DB。"""
    values = dict(_env if env_values is None else env_values)
    conn = db.get_conn()
    marker = conn.execute("SELECT value FROM app_meta WHERE key=?", (MIGRATION_META_KEY,)).fetchone()
    if marker:
        return {"status": "already_completed", "capabilities": []}
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM model_providers), (SELECT COUNT(*) FROM model_configs)"
    ).fetchone()
    if int(counts[0]) or int(counts[1]):
        return {"status": "skipped_existing_config", "capabilities": []}

    imported: list[str] = []
    specs = (("chat", "CHAT"), ("vision", "VISION"), ("image_gen", "IMAGE_GEN"))
    try:
        for priority, (role, prefix) in enumerate(specs, start=1):
            cfg = _legacy_role(values, role, prefix)
            if not cfg.ready:
                continue
            cur = conn.execute(
                """INSERT INTO model_providers(name,type,base_url,api_key_ciphertext,auth_type,extra_headers_json,enabled,note)
                   VALUES(?,?,?,?,?,?,1,?)""",
                (
                    f"旧配置 · {CAPABILITY_LABELS[role]}",
                    "openai_compatible",
                    cfg.base_url,
                    cfg.api_key,
                    "custom_headers" if cfg.extra_headers else "bearer",
                    json.dumps(cfg.extra_headers, ensure_ascii=False),
                    "由旧 .env 一次性安全迁移；后续请在页面管理",
                ),
            )
            conn.execute(
                """INSERT INTO model_configs(provider_id,model_name,capability,enabled,priority,params_json,note)
                   VALUES(?,?,?,1,?,'{}',?)""",
                (cur.lastrowid, cfg.model, role, priority, "由旧 .env 一次性安全迁移"),
            )
            imported.append(role)
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES(?,?)",
            (MIGRATION_META_KEY, json.dumps({"capabilities": imported}, ensure_ascii=False)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"status": "imported", "capabilities": imported}


def classify_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text or "auth" in text:
        return "auth_error"
    if "404" in text or ("model" in text and "not found" in text):
        return "model_not_found"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "json" in text or "response" in text:
        return "bad_response"
    return "network"


async def test_model_connection(
    capability: str,
    provider_id: int,
    model_name: str,
    sample: str | None = None,
) -> dict:
    """按 capability/provider/model 测试连接，返回统一 ok/error 结构。"""
    import httpx2 as httpx
    from openai import AsyncOpenAI

    conn = db.get_conn()
    row = conn.execute("SELECT * FROM model_providers WHERE id=?", (provider_id,)).fetchone()
    if not row:
        return {"ok": False, "error_type": "not_found", "message": "Provider 不存在"}
    cfg = ProviderConfig(
        role=capability,
        base_url=(row["base_url"] or "").rstrip("/"),
        api_key=row["api_key_ciphertext"] or "",
        model=model_name,
        extra_headers=_load_json(row["extra_headers_json"], {}),
    )
    if not cfg.ready:
        return {"ok": False, "error_type": "bad_response", "message": "Provider 缺少 Base URL、API Key 或模型名"}

    prompt = sample or "请用一句话回复：连接测试成功。"
    t0 = time.monotonic()
    try:
        if capability in ("chat", "vision"):
            client = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                default_headers=cfg.extra_headers or None,
                timeout=20.0,
                max_retries=0,
                http_client=httpx.AsyncClient(trust_env=False, timeout=20.0),
            )
            resp = await client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=64,
            )
            out = resp.choices[0].message.content or ""
        elif capability == "image_gen":
            async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
                resp = await client.post(
                    f"{cfg.base_url}/images/generations",
                    headers={"Authorization": f"Bearer {cfg.api_key}", **cfg.extra_headers},
                    json={"model": model_name, "prompt": prompt, "size": "1024x1024", "n": 1},
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                data = resp.json()
                out = "image generated" if data.get("data") else ""
        else:
            return {"ok": False, "error_type": "bad_response", "message": f"不支持的能力：{capability}"}
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "message": "连接成功", "sample_output": out[:300]}
    except Exception as exc:  # noqa: BLE001
        latency = int((time.monotonic() - t0) * 1000)
        return {
            "ok": False,
            "latency_ms": latency,
            "error_type": classify_error(exc),
            "message": "连接失败，请检查 Provider、模型名、鉴权和网络设置",
        }
