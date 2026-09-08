"""P2b 模型与 API 设置：DB 唯一配置源、旧 .env 迁移与脱敏测试（不触网）。"""
import asyncio
import inspect
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from fastapi import HTTPException

import db
import main
import providers
import vision


FAKE_ENV = {
    "CHAT_BASE_URL": "https://chat.example.test/v1",
    "CHAT_API_KEY": "example-key",
    "CHAT_MODEL": "chat-test-model",
    "VISION_BASE_URL": "https://vision.example.test/v1",
    "VISION_API_KEY": "example-key",
    "VISION_MODEL": "vision-test-model",
    "VISION_EXTRA_HEADERS": '{"Example-Access-Client-Id":"client-id-test","Example-Access-Client-Secret":"client-secret-test"}',
    "IMAGE_GEN_BASE_URL": "https://image.example.test/v1",
    "IMAGE_GEN_API_KEY": "example-key",
    "IMAGE_GEN_MODEL": "image-test-model",
    "IMAGE_GEN_EXTRA_HEADERS": '{"Example-Access-Client-Id":"client-id-test","Example-Access-Client-Secret":"client-secret-test"}',
}


class ModelSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        db.DATA_DIR = Path(self.tmp.name)
        db._local = threading.local()
        db.init_db()
        self._reset_vision_queue()

    def _reset_vision_queue(self):
        vision._queue = None
        vision._worker_task = None
        vision._background_tasks.clear()
        vision._queued_doc_ids.clear()
        vision._queued_fig_doc_ids.clear()
        vision._running_doc_id = None
        vision._running_job = None

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            del db._local.conn
        self._reset_vision_queue()
        self.tmp.cleanup()

    def _provider(self, api_key="example-key", enabled=True, extra_headers=None) -> int:
        with mock.patch("providers.migrate_legacy_env_config", return_value={"status": "skipped_existing_config", "capabilities": []}):
            with TestClient(main.app) as client:
                res = client.post("/api/model-providers", json={
                    "name": "Test Provider",
                    "type": "openai_compatible",
                    "base_url": "https://api.example.test/v1/",
                    "api_key": api_key,
                    "auth_type": "bearer",
                    "extra_headers": extra_headers or {"X-Test": "1"},
                    "enabled": enabled,
                    "note": "n",
                })
        self.assertEqual(res.status_code, 200, res.text)
        return int(res.json()["provider_id"])

    def _model(self, provider_id: int, capability="chat", priority=10, enabled=True, name="test-model") -> int:
        with mock.patch("providers.migrate_legacy_env_config", return_value={"status": "skipped_existing_config", "capabilities": []}):
            with TestClient(main.app) as client:
                res = client.post("/api/model-configs", json={
                    "provider_id": provider_id,
                    "model_name": name,
                    "capability": capability,
                    "enabled": enabled,
                    "priority": priority,
                    "params": {"temperature": 0.2},
                    "note": "m",
                })
        self.assertEqual(res.status_code, 200, res.text)
        return int(res.json()["model_config_id"])

    def _document(self) -> None:
        conn = db.get_conn()
        conn.execute("INSERT INTO subjects(id,name,sort_order) VALUES(1,'数学',0)")
        conn.execute(
            "INSERT INTO documents(id,subject_id,folder_id,filename,orig_path,pdf_path,page_count,sort_order)"
            " VALUES(1,1,NULL,'讲义.pdf','','',1,0)"
        )
        conn.execute(
            "INSERT INTO document_pages(doc_id,page_no,text,lang,source,width,height)"
            " VALUES(1,1,'测试内容','zh','text',595,842)"
        )
        conn.commit()

    def _chat_events(self, model_config_id=None):
        captured = []

        async def fake_stream(messages, usage_out=None, provider_config=None):
            captured.append(provider_config)
            yield "ok"

        setattr(fake_stream, "__signature__", inspect.signature(main.stream_chat))

        async def run():
            with mock.patch.object(main, "stream_chat", fake_stream):
                response = await main.chat(main.ChatIn(
                    doc_id=1,
                    pages=[1],
                    question="解释",
                    history=None,
                    model_config_id=model_config_id,
                ))
                events = []
                async for chunk in response.body_iterator:
                    for block in chunk.split("\n\n"):
                        if not block.strip():
                            continue
                        lines = block.split("\n")
                        events.append(json.loads(lines[1][len("data: "):]))
                return events

        return asyncio.run(run()), captured

    def test_empty_db_never_falls_back_to_env_and_error_is_actionable(self):
        with mock.patch.dict(providers._env, FAKE_ENV, clear=True):
            with self.assertRaises(providers.ModelConfigurationError) as caught:
                providers.get_provider("chat")
        self.assertEqual(caught.exception.capability, "chat")
        self.assertIn("设置→模型与 API", str(caught.exception))
        self.assertNotIn(".env", str(caught.exception))

    def test_first_migration_imports_chat_vision_and_image_gen(self):
        result = providers.migrate_legacy_env_config(FAKE_ENV)
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["capabilities"], ["chat", "vision", "image_gen"])
        rows = db.get_conn().execute(
            "SELECT capability, model_name FROM model_configs ORDER BY priority, id"
        ).fetchall()
        self.assertEqual(
            [(r["capability"], r["model_name"]) for r in rows],
            [("chat", "chat-test-model"), ("vision", "vision-test-model"), ("image_gen", "image-test-model")],
        )

    def test_migration_is_idempotent(self):
        first = providers.migrate_legacy_env_config(FAKE_ENV)
        second = providers.migrate_legacy_env_config(FAKE_ENV)
        self.assertEqual(first["status"], "imported")
        self.assertEqual(second["status"], "already_completed")
        conn = db.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_configs").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_providers").fetchone()[0], 3)

    def test_migration_does_not_touch_any_existing_page_configuration(self):
        provider_id = self._provider()
        self._model(provider_id, capability="chat", priority=77, name="user-model")
        before = [tuple(r) for r in db.get_conn().execute(
            "SELECT id, provider_id, model_name, capability, priority FROM model_configs ORDER BY id"
        )]
        result = providers.migrate_legacy_env_config(FAKE_ENV)
        after = [tuple(r) for r in db.get_conn().execute(
            "SELECT id, provider_id, model_name, capability, priority FROM model_configs ORDER BY id"
        )]
        self.assertEqual(result["status"], "skipped_existing_config")
        self.assertEqual(after, before)

    def test_migrated_get_provider_uses_database_values(self):
        providers.migrate_legacy_env_config(FAKE_ENV)
        with mock.patch.dict(providers._env, {}, clear=True):
            cfg = providers.get_provider("vision")
        self.assertEqual(cfg.model, "vision-test-model")
        self.assertEqual(cfg.base_url, "https://vision.example.test/v1")
        self.assertEqual(cfg.api_key, "example-key")
        self.assertEqual(cfg.extra_headers["Example-Access-Client-Secret"], "client-secret-test")
        self.assertIsNotNone(cfg.model_config_id)

    def test_missing_capability_error_is_actionable_even_when_other_capability_exists(self):
        provider_id = self._provider()
        self._model(provider_id, capability="chat")
        with self.assertRaises(providers.ModelConfigurationError) as caught:
            providers.get_provider("vision")
        self.assertEqual(caught.exception.capability, "vision")
        self.assertIn("识图", str(caught.exception))
        self.assertIn("配置并启用", str(caught.exception))

    def test_provider_api_masks_key_and_sensitive_extra_headers(self):
        secret = "test-secret"
        self._provider(
            api_key="example-key",
            extra_headers={
                "Example-Access-Client-Id": "client-id-value",
                "Example-Access-Client-Secret": secret,
                "Authorization": "Bearer nested-secret",
                "X-Region": "test-region",
            },
        )
        with TestClient(main.app) as client:
            res = client.get("/api/model-providers")
        self.assertEqual(res.status_code, 200)
        raw = res.text
        self.assertNotIn("example-key", raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("nested-secret", raw)
        item = res.json()[0]
        self.assertNotIn("api_key", item)
        self.assertNotIn("api_key_ciphertext", item)
        self.assertEqual(item["extra_headers"]["X-Region"], "test-region")
        self.assertIn("****", item["extra_headers"]["Example-Access-Client-Secret"])
        self.assertIn("****", item["extra_headers"]["Authorization"])

    def test_patch_empty_or_masked_secrets_do_not_overwrite_stored_values(self):
        provider_id = self._provider(
            api_key="example-key",
            extra_headers={"Example-Access-Client-Secret": "original-header-secret", "X-Region": "old"},
        )
        with TestClient(main.app) as client:
            shown = client.get("/api/model-providers").json()[0]
            res = client.patch(f"/api/model-providers/{provider_id}", json={
                "api_key": "",
                "extra_headers": {
                    "Example-Access-Client-Secret": shown["extra_headers"]["Example-Access-Client-Secret"],
                    "X-Region": "new",
                },
            })
        self.assertEqual(res.status_code, 200, res.text)
        row = db.get_conn().execute("SELECT * FROM model_providers WHERE id=?", (provider_id,)).fetchone()
        self.assertEqual(row["api_key_ciphertext"], "example-key")
        headers = providers._load_json(row["extra_headers_json"], {})
        self.assertEqual(headers["Example-Access-Client-Secret"], "original-header-secret")
        self.assertEqual(headers["X-Region"], "new")

    def test_model_configs_filter_by_capability_and_sort_by_priority(self):
        provider_id = self._provider()
        self._model(provider_id, capability="chat", priority=20, name="slow")
        self._model(provider_id, capability="vision", priority=1, name="vision")
        self._model(provider_id, capability="chat", priority=5, name="fast")
        with TestClient(main.app) as client:
            res = client.get("/api/model-configs?capability=chat")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([x["model_name"] for x in res.json()], ["fast", "slow"])

    def test_get_provider_uses_enabled_db_model_by_lowest_priority(self):
        provider_id = self._provider(api_key="sk-db")
        self._model(provider_id, capability="chat", priority=100, name="backup")
        self._model(provider_id, capability="chat", priority=1, name="primary")
        cfg = providers.get_provider("chat")
        self.assertEqual(cfg.model, "primary")
        self.assertEqual(cfg.base_url, "https://api.example.test/v1")

    def test_chat_with_model_config_id_uses_that_exact_configuration(self):
        self._document()
        first_provider = self._provider(api_key="sk-first")
        second_provider = self._provider(api_key="sk-second")
        self._model(first_provider, priority=1, name="default-model")
        selected_id = self._model(second_provider, priority=50, name="selected-model")

        events, captured = self._chat_events(selected_id)

        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].model_config_id, selected_id)
        self.assertEqual(captured[0].model, "selected-model")
        self.assertEqual(captured[0].api_key, "sk-second")

    def test_chat_without_model_config_id_uses_default_lowest_priority(self):
        self._document()
        provider_id = self._provider()
        self._model(provider_id, priority=20, name="backup")
        default_id = self._model(provider_id, priority=1, name="primary")

        _, captured = self._chat_events()

        self.assertEqual(captured[0].model_config_id, default_id)
        self.assertEqual(captured[0].model, "primary")

    def test_chat_rejects_unknown_model_config_without_fallback(self):
        self._document()
        provider_id = self._provider()
        self._model(provider_id, priority=1, name="default")
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.chat(main.ChatIn(
                doc_id=1, pages=[1], question="解释", history=None, model_config_id=999999,
            )))
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("不存在", str(caught.exception.detail))

    def test_chat_rejects_non_chat_model_config_without_fallback(self):
        self._document()
        provider_id = self._provider()
        self._model(provider_id, capability="chat", priority=1, name="default")
        selected_id = self._model(provider_id, capability="vision", priority=2, name="vision-only")
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.chat(main.ChatIn(
                doc_id=1, pages=[1], question="解释", history=None, model_config_id=selected_id,
            )))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("推理", str(caught.exception.detail))

    def test_chat_rejects_disabled_model_without_fallback(self):
        self._document()
        provider_id = self._provider()
        self._model(provider_id, priority=1, name="default")
        selected_id = self._model(provider_id, priority=2, enabled=False, name="disabled")
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.chat(main.ChatIn(
                doc_id=1, pages=[1], question="解释", history=None, model_config_id=selected_id,
            )))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("已禁用", str(caught.exception.detail))

    def test_chat_rejects_disabled_provider_without_fallback(self):
        self._document()
        default_provider = self._provider()
        disabled_provider = self._provider(enabled=False)
        self._model(default_provider, priority=1, name="default")
        selected_id = self._model(disabled_provider, priority=2, name="provider-disabled")
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.chat(main.ChatIn(
                doc_id=1, pages=[1], question="解释", history=None, model_config_id=selected_id,
            )))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Provider", str(caught.exception.detail))
        self.assertIn("禁用", str(caught.exception.detail))

    def test_test_endpoint_returns_structured_error_for_invalid_connection(self):
        provider_id = self._provider(api_key="bad-key")
        with mock.patch("providers.test_model_connection", return_value={
            "ok": False,
            "error_type": "auth_error",
            "message": "认证失败",
            "latency_ms": 12,
        }) as fake:
            with TestClient(main.app) as client:
                res = client.post("/api/model-configs/test", json={
                    "capability": "chat",
                    "provider_id": provider_id,
                    "model_name": "bad-model",
                    "sample": "ping",
                })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["ok"])
        self.assertEqual(res.json()["error_type"], "auth_error")
        fake.assert_called_once()


if __name__ == "__main__":
    unittest.main()
