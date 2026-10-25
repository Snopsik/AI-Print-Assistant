import base64
import importlib.util
import json
import pathlib
import sys
import time
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent
PLUGIN_PATH = ROOT / "orca_ai_assistant_vision_win_x86_64.py"
IMAGE = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()


class PagesBase:
    def __init__(self):
        self.base_initialized = True
        self._stored_config = "{}"
        self.outbox = []
        self.save_calls = 0

    def get_config(self):
        return self._stored_config

    def save_config(self, value):
        json.loads(value)
        self._stored_config = value
        self.save_calls += 1
        return True

    def post_message(self, value):
        self.outbox.append(value)


orca = types.ModuleType("orca")
orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=PagesBase)
orca.base = type("Base", (), {})
orca.plugin = lambda cls: cls
orca.register_capability = mock.Mock()
orca.request_permissions = mock.Mock(side_effect=TypeError("Alpha only supports fs_read"))
orca.host = types.SimpleNamespace(
    app_language=lambda: "ru_RU",
    preset_bundle=lambda: None,
    model=lambda: types.SimpleNamespace(objects=lambda: []),
)
sys.modules["orca"] = orca
spec = importlib.util.spec_from_file_location("orca_ai_assistant_vision", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def config(mode="separate"):
    return {
        "mode": mode,
        "text_provider": "custom",
        "text_endpoint": "https://text.test/v1/chat/completions",
        "text_model": "logic-model",
        "text_api_key": "text-secret",
        "vision_provider": "custom",
        "vision_endpoint": "https://vision.test/v1/chat/completions",
        "vision_model": "vision-model",
        "vision_api_key": "vision-secret",
    }


def result(verify=False, action="none"):
    return json.dumps({
        "answer": "Итоговый ответ",
        "needs_vision_verification": verify,
        "vision_question": "Что именно видно у края детали?" if verify else "",
        "risk": "critical" if action == "stop" else "none",
        "proposed_action": action,
        "confirmation_required": action != "none",
    })


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit):
        return json.dumps(self.payload).encode()


class VisionPluginTests(unittest.TestCase):
    def test_compile_and_alpha_registration(self):
        compile(PLUGIN_PATH.read_text(encoding="utf-8"), str(PLUGIN_PATH), "exec")
        package = plugin.AIPrintAssistantPlugin()
        package.register_capabilities()
        orca.register_capability.assert_called_with(plugin.AIAssistantPage)
        page = plugin.AIAssistantPage()
        self.assertTrue(page.base_initialized)

    def test_models_endpoint(self):
        self.assertEqual(
            plugin.models_endpoint("https://host/v1/chat/completions"),
            "https://host/v1/models",
        )
        self.assertEqual(
            plugin.models_endpoint("https://host/v1/responses"),
            "https://host/v1/models",
        )

    def test_provider_presets_and_custom_endpoint(self):
        self.assertEqual(plugin.provider_for_endpoint(plugin.PROVIDER_ENDPOINTS["openai"]), "openai")
        self.assertEqual(plugin.provider_for_endpoint(plugin.PROVIDER_ENDPOINTS["gemini"]), "gemini")
        self.assertEqual(plugin.provider_for_endpoint(plugin.PROVIDER_ENDPOINTS["glm"]), "glm")
        self.assertEqual(plugin.provider_for_endpoint(plugin.PROVIDER_ENDPOINTS["openrouter"]), "openrouter")
        self.assertEqual(
            plugin.provider_for_endpoint("https://my-server.example/v1/chat/completions"),
            "custom",
        )
        self.assertIn("value=\"custom\">Свой endpoint</option>", plugin.PAGE)
        self.assertIn("id=\"text_custom_endpoint\" hidden", plugin.PAGE)
        self.assertIn("id=\"vision_custom_endpoint\" hidden", plugin.PAGE)

    def test_model_discovery_and_vision_tags(self):
        payload = {"data": [
            {"id": "gpt-4.1-mini"},
            {"id": "text-embedding-3-small"},
            {"id": "custom", "input_modalities": ["text", "image"]},
            {"id": "mystery"},
        ]}
        with mock.patch.object(plugin.urllib.request, "urlopen", return_value=FakeResponse(payload)) as call:
            models = plugin.discover_models({
                "endpoint": "https://host/v1/chat/completions", "api_key": "secret"
            })
        self.assertEqual(call.call_args.args[0].full_url, "https://host/v1/models")
        tags = {item["id"]: item["vision"] for item in models}
        self.assertEqual(tags, {
            "custom": "yes", "gpt-4.1-mini": "yes",
            "mystery": "unknown", "text-embedding-3-small": "no",
        })

    def test_unified_vision_uses_one_call(self):
        with mock.patch.object(plugin, "request_chat", return_value=result()) as request:
            answer = plugin.run_analysis(config("unified"), "Что на фото?", {}, [], IMAGE)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["image_data"], IMAGE)
        self.assertEqual(answer["answer"], "Итоговый ответ")

    def test_separate_models_share_report(self):
        reports = ["На кадре возможно отлипание.", result()]
        with mock.patch.object(plugin, "request_chat", side_effect=reports) as request:
            answer = plugin.run_analysis(config(), "Проверь", {}, [], IMAGE)
        self.assertEqual(request.call_count, 2)
        second_messages = request.call_args_list[1].args[1]
        self.assertIn("На кадре возможно отлипание.", second_messages[-1]["content"])
        self.assertEqual(answer["answer"], "Итоговый ответ")

    def test_targeted_verification_round_trip(self):
        reports = ["Первый отчёт", result(verify=True), "Уточнённый отчёт", result(action="stop")]
        with mock.patch.object(plugin, "request_chat", side_effect=reports) as request:
            answer = plugin.run_analysis(config(), "Проверь", {}, [], IMAGE)
        self.assertEqual(request.call_count, 4)
        third_messages = request.call_args_list[2].args[1]
        self.assertIn("Что именно видно у края детали?", third_messages[-1]["content"])
        fourth_messages = request.call_args_list[3].args[1]
        self.assertIn("Уточнённый отчёт", fourth_messages[-1]["content"])
        self.assertFalse(answer["needs_vision_verification"])
        self.assertTrue(answer["confirmation_required"])

    def test_image_is_attached_as_openai_content(self):
        captured = {}

        def fake_urlopen(request, **kwargs):
            captured.update(json.loads(request.data.decode()))
            return FakeResponse({"choices": [{"message": {"content": result()}}]})

        with mock.patch.object(plugin.urllib.request, "urlopen", side_effect=fake_urlopen):
            plugin.request_chat({
                "endpoint": "https://host/v1/chat/completions",
                "model": "vision", "api_key": "secret",
            }, [{"role": "user", "content": "Проверь"}], image_data=IMAGE)
        content = captured["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["image_url"]["url"], IMAGE)

    def test_action_requires_confirmation_and_never_calls_printer(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        page._stored_config = json.dumps(config())
        analysis = plugin.normalize_result(result(action="stop"))
        with mock.patch.object(plugin, "run_analysis", return_value=analysis):
            page.on_message({"type": "ask", "text": "Опасно?", "include_context": False})
            deadline = time.time() + 2
            while not any(item.get("type") == "answer" for item in page.outbox) and time.time() < deadline:
                time.sleep(0.01)
        answer = next(item for item in page.outbox if item.get("type") == "answer")
        action_id = answer["action_id"]
        self.assertIsNotNone(action_id)
        page.on_message({"type": "action_confirm", "action_id": action_id})
        confirmation = page.outbox[-1]
        self.assertEqual(confirmation["type"], "action_result")
        self.assertIn("вручную", confirmation["text"])
        self.assertNotIn(action_id, page._pending_actions)

    def test_settings_saved_once_and_keys_hidden(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        payload = config()
        payload.update({"type": "save_settings"})
        page.on_message(payload)
        self.assertEqual(page.save_calls, 1)
        self.assertEqual(page.outbox[-1]["type"], "settings_saved")
        public = page._public_config()
        self.assertNotIn("text-secret", json.dumps(public))
        self.assertNotIn("vision-secret", json.dumps(public))
        self.assertTrue(public["has_text_key"])
        self.assertTrue(public["has_vision_key"])

    def test_stop_always_requires_confirmation(self):
        parsed = plugin.normalize_result(json.dumps({
            "answer": "Остановите", "risk": "critical",
            "proposed_action": "stop", "confirmation_required": False,
        }))
        self.assertTrue(parsed["confirmation_required"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
