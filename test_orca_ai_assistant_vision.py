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


class SlicingBase:
    pass


class ExecutionResult:
    @staticmethod
    def success(message="", data=""):
        return types.SimpleNamespace(status="success", message=message, data=data)

    @staticmethod
    def skipped(message=""):
        return types.SimpleNamespace(status="skipped", message=message, data="")

    @staticmethod
    def failure(status, message, data=""):
        return types.SimpleNamespace(status=status, message=message, data=data)


orca = types.ModuleType("orca")
orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=PagesBase)
orca.slicing = types.SimpleNamespace(
    SlicingPipelineCapabilityBase=SlicingBase,
    Step=types.SimpleNamespace(psGCodePostProcess="postprocess"),
)
orca.ExecutionResult = ExecutionResult
orca.PluginResult = types.SimpleNamespace(RecoverableError="recoverable")
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
        orca.register_capability.assert_any_call(plugin.AIAssistantPage)
        orca.register_capability.assert_any_call(plugin.AIGCodeReviewCapability)
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

    def test_normalize_chat_endpoint(self):
        self.assertEqual(
            plugin.normalize_chat_endpoint("https://api.dolphinllm.com/v1"),
            "https://api.dolphinllm.com/v1/chat/completions",
        )
        self.assertEqual(
            plugin.normalize_chat_endpoint("https://host/v1/chat/completions"),
            "https://host/v1/chat/completions",
        )
        self.assertEqual(
            plugin.normalize_chat_endpoint("https://host/v1?key=value"),
            "https://host/v1/chat/completions?key=value",
        )

    def test_collect_context_without_app_language(self):
        with mock.patch.object(
            plugin.orca.host, "app_language", side_effect=AttributeError
        ):
            context = plugin.collect_context()
        self.assertEqual(context["orca_ui_language"], "ru_RU")

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

    def test_setting_changes_are_whitelisted_and_bounded(self):
        parsed = plugin.normalize_result(json.dumps({
            "answer": "Оптимизация",
            "setting_changes": [
                {"category": "process", "key": "outer_wall_speed", "value": 45},
                {"category": "process", "key": "outer_wall_speed", "value": 1000},
                {"category": "printer", "key": "printer_model", "value": "other"},
                {"category": "printer", "key": "layer_height", "value": 0.2},
            ],
        }))
        self.assertEqual(len(parsed["setting_changes"]), 1)
        self.assertEqual(parsed["setting_changes"][0]["value"], "45")

    def test_apply_changes_uses_orca_transactions(self):
        transactions = []

        class Transaction:
            def __init__(self, category):
                self.category = category
                self.values = {}
                self.target_name = category + " profile"

            def set(self, key, value):
                self.values[key] = value

            def commit(self):
                transactions.append(self)
                return self

        class Editor:
            def __init__(self, category):
                self.category = category
                self.args = None

            def begin_update(self, *args):
                self.args = args
                return Transaction(self.category)

        editors = {name: Editor(name) for name in ("process", "filament", "printer")}
        with mock.patch.object(plugin.orca.host, "preset", types.SimpleNamespace(**editors), create=True):
            applied = plugin.apply_setting_changes([
                {"category": "process", "key": "layer_height", "value": "0.2"},
                {"category": "filament", "key": "fan_max_speed", "value": "90"},
            ])
        self.assertEqual(applied, ["process profile", "filament profile"])
        self.assertEqual(editors["filament"].args, (1,))
        self.assertEqual(transactions[1].values, {"fan_max_speed": "90"})

    def test_gcode_summary_and_previous_analysis_are_reviewed(self):
        summary = plugin.summarize_gcode(
            ";LAYER_CHANGE\nG1 Z0.2 F1200\nM104 S210\nM106 S128\nG1 X10 E1.2 F1800\nM112\n"
        )
        self.assertEqual(summary["layer_markers"], 1)
        self.assertEqual(summary["max_z_mm"], 0.2)
        self.assertEqual(summary["temperatures_c"]["M104"], [210.0])
        self.assertTrue(summary["alerts"])
        with mock.patch.object(plugin, "request_chat", return_value=result()) as request:
            reviewed = plugin.run_gcode_review(config(), summary, {"answer": "Нужно больше обдува"})
        packet = request.call_args.args[1][-1]["content"]
        self.assertIn("Нужно больше обдува", packet)
        self.assertEqual(reviewed["setting_changes"], [])

    def test_camera_snapshot_validation_and_decoding(self):
        class CameraResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, limit):
                return b"\xff\xd8\xffcamera"

            def geturl(self):
                return "http://printer.local/snapshot.jpg"

        with mock.patch.object(plugin.urllib.request, "urlopen", return_value=CameraResponse()):
            image = plugin.fetch_camera_snapshot("http://printer.local/snapshot.jpg")
        self.assertTrue(image.startswith("data:image/jpeg;base64,"))
        with self.assertRaises(ValueError):
            plugin.validate_monitor_url("https://user:secret@example.test/snapshot")

    def test_prepare_preview_dock_panel_is_created(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        dock = mock.Mock()
        creator = mock.Mock(return_value=dock)
        with mock.patch.object(plugin.orca.host, "ui",
                               types.SimpleNamespace(create_dock_panel=creator), create=True):
            page.on_load()
        self.assertEqual(creator.call_args.kwargs["html"], plugin.DOCK_PAGE)
        self.assertEqual(creator.call_args.kwargs["dock"], "right")
        self.assertIn("dock_ready", plugin.DOCK_PAGE)

    def test_pipeline_reviews_exported_gcode(self):
        import tempfile
        page = mock.Mock()
        with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False,
                                         encoding="utf-8") as handle:
            handle.write("G28\nG1 Z0.2 E1\n")
            path = handle.name
        try:
            with mock.patch.object(plugin, "ACTIVE_PAGE", page):
                outcome = plugin.AIGCodeReviewCapability().execute(types.SimpleNamespace(
                    step=plugin.orca.slicing.Step.psGCodePostProcess, gcode_path=path,
                ))
        finally:
            pathlib.Path(path).unlink()
        self.assertEqual(outcome.status, "success")
        self.assertEqual(page.review_gcode_summary.call_args.args[0]["lines"], 2)

    def test_monitor_start_requires_snapshot_url(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        page.on_message({"type": "monitor_start"})
        self.assertEqual(page.outbox[-1]["type"], "error")
        self.assertIn("URL камеры", page.outbox[-1]["text"])

    def test_monitoring_analyzes_camera_snapshot(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        stored = config()
        stored.update({"monitor_url": "http://printer.local/snapshot", "monitor_interval": 30})
        page._stored_config = json.dumps(stored)

        def analyzed(*args, **kwargs):
            stored["monitor_enabled"] = False
            page._stored_config = json.dumps(stored)
            return plugin.normalize_result(result())

        with mock.patch.object(plugin, "fetch_camera_snapshot", return_value=IMAGE), \
                mock.patch.object(plugin, "run_analysis", side_effect=analyzed):
            page.on_message({"type": "monitor_start"})
            deadline = time.time() + 2
            while not any(item.get("type") == "monitor_answer" for item in page.outbox) \
                    and time.time() < deadline:
                time.sleep(0.01)
        page._stop_monitor()
        self.assertTrue(any(item.get("type") == "monitor_answer" for item in page.outbox))

    def test_stop_always_requires_confirmation(self):
        parsed = plugin.normalize_result(json.dumps({
            "answer": "Остановите", "risk": "critical",
            "proposed_action": "stop", "confirmation_required": False,
        }))
        self.assertTrue(parsed["confirmation_required"])

    def test_safe_markdown_renderer_in_both_interfaces(self):
        for page in (plugin.PAGE, plugin.DOCK_PAGE):
            self.assertIn("function renderMarkdown(container,value)", page)
            self.assertIn("function appendInline(parent,value)", page)
            self.assertIn("function plainMath(value)", page)
            self.assertIn("md-list-item", page)
            self.assertNotIn("innerHTML", page)
        self.assertNotIn("latest.textContent=data.text", plugin.DOCK_PAGE)
        self.assertIn("showAnswer(data.text", plugin.DOCK_PAGE)

    def test_full_width_layout_and_scrollable_dock(self):
        self.assertIn(".msg{width:100%;max-width:none", plugin.PAGE)
        self.assertIn("#changesList{display:grid", plugin.PAGE)
        self.assertIn("max-width:110ch", plugin.PAGE)
        self.assertIn("#dockScroll{flex:1;min-height:0;overflow:auto", plugin.DOCK_PAGE)
        self.assertIn("html,body{height:100%;overflow:hidden}", plugin.DOCK_PAGE)
        self.assertIn("<details><summary>Автоматическая проверка G-code</summary>", plugin.DOCK_PAGE)

    def test_structured_prompts_and_readable_math(self):
        self.assertIn("«### Наблюдения»", plugin.LOGIC_PROMPT)
        self.assertIn("«### Что сделать»", plugin.LOGIC_PROMPT)
        self.assertIn("«### Замечания»", plugin.GCODE_PROMPT)
        self.assertIn("Не\nиспользуй LaTeX", plugin.LOGIC_PROMPT)
        for page in (plugin.PAGE, plugin.DOCK_PAGE):
            self.assertIn("replace(/\\\\times\\b/g,'×')", page)
            self.assertIn("replace(/\\$/g,'')", page)

    def test_auto_apply_setting_is_saved_as_boolean(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        payload = config()
        payload.update({"type": "save_settings", "auto_apply": True})
        page.on_message(payload)
        stored = json.loads(page._stored_config)
        self.assertIs(stored["auto_apply"], True)
        self.assertIs(page._public_config()["auto_apply"], True)

    def test_text_recommendations_are_extracted_and_whitelisted(self):
        payload = json.dumps({"setting_changes": [
            {"category": "process", "key": "sparse_infill_speed", "value": 100},
            {"category": "printer", "key": "printer_model", "value": "other"},
        ]})
        with mock.patch.object(plugin, "request_chat", return_value=payload) as request:
            changes = plugin.extract_recommended_changes(
                config(), {"answer": "Уменьшите скорость заполнения до 100 мм/с"},
                {"print_settings": {"sparse_infill_speed": 180}},
            )
        self.assertEqual(changes[0]["value"], "100")
        self.assertEqual(len(changes), 1)
        self.assertIn("Уменьшите скорость", request.call_args.args[1][-1]["content"])

    def test_apply_text_suggestions_and_restore_previous_value(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        page._last_analysis = {"answer": "Уменьшите sparse_infill_speed до 100"}
        extracted = [{"category": "process", "key": "sparse_infill_speed", "value": "100"}]
        with mock.patch.object(plugin, "collect_context", return_value={
            "print_settings": {"sparse_infill_speed": 180},
        }), mock.patch.object(plugin, "extract_recommended_changes", return_value=extracted), \
                mock.patch.object(plugin, "apply_setting_changes", return_value=["process profile"]) as apply:
            page._run_apply_suggestions(0)
            self.assertEqual(apply.call_args.args[0][0]["old"], 180)
            self.assertEqual(page._rollback_changes[0]["value"], "180")
            page._run_rollback_changes(page._rollback_changes)
        self.assertEqual(apply.call_args_list[1].args[0][0]["value"], "180")
        self.assertEqual(page._rollback_changes, [])
        self.assertTrue(page.outbox[-1]["success"])

    def test_auto_apply_runs_after_structured_analysis(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        analysis = plugin.normalize_result(json.dumps({
            "answer": "Снижаю скорость",
            "setting_changes": [{"category": "process", "key": "sparse_infill_speed", "value": 100}],
        }))
        automatic_config = config()
        automatic_config["auto_apply"] = True
        with mock.patch.object(plugin, "run_analysis", return_value=analysis), \
                mock.patch.object(plugin, "apply_setting_changes", return_value=["process profile"]):
            page._run_request(automatic_config, "Оптимизируй", {
                "print_settings": {"sparse_infill_speed": 180},
            }, [], "", 0)
        applied = next(item for item in page.outbox if item.get("type") == "apply_result")
        self.assertTrue(applied["success"])
        self.assertTrue(applied["automatic"])
        self.assertTrue(applied["can_rollback"])

    def test_failed_apply_keeps_rollback_available(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        changes = [{"category": "process", "key": "layer_height", "value": "0.2", "old": 0.16}]
        with mock.patch.object(plugin, "apply_setting_changes", side_effect=RuntimeError("commit failed")):
            page._run_apply_changes(changes)
        self.assertFalse(page.outbox[-1]["success"])
        self.assertTrue(page.outbox[-1]["can_rollback"])
        self.assertEqual(page._rollback_changes[0]["value"], "0.16")

    def test_apply_and_rollback_controls_exist_in_both_interfaces(self):
        for page in (plugin.PAGE, plugin.DOCK_PAGE):
            self.assertIn("id=\"applySuggested\"", page)
            self.assertIn("id=\"rollbackChanges\" hidden", page)
            self.assertIn("type:'apply_suggestions'", page)
            self.assertIn("type:'rollback_changes'", page)
        self.assertIn("id=\"auto_apply\"", plugin.PAGE)
        self.assertIn("payload.auto_apply=document.getElementById('auto_apply').checked", plugin.PAGE)

    def test_printer_modifications_are_saved_and_added_to_context(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        mods = "Тестовый принтер; другое сопло"
        payload = config()
        payload.update({"type": "save_settings", "printer_mods": "  " + mods + "  "})
        page.on_message(payload)
        self.assertEqual(json.loads(page._stored_config)["printer_mods"], mods)
        self.assertEqual(page._public_config()["printer_mods"], mods)
        self.assertEqual(plugin.collect_context(mods)["printer_modifications"], mods)

    def test_printer_modifications_reach_recommendations_and_gcode_review(self):
        configured = config()
        configured["printer_mods"] = "Тестовый принтер; другое сопло"
        payload = json.dumps({"setting_changes": []})
        with mock.patch.object(plugin, "request_chat", return_value=payload) as request:
            plugin.extract_recommended_changes(configured, {"answer": "Проверьте расход"}, {})
        self.assertIn("другое сопло", request.call_args.args[1][-1]["content"])
        with mock.patch.object(plugin, "request_chat", return_value=result()) as request:
            plugin.run_gcode_review(configured, {"lines": 10})
        self.assertIn("другое сопло", request.call_args.args[1][-1]["content"])

    def test_printer_modifications_ui_default_and_prompt_priority(self):
        self.assertIn('textarea id="printer_mods"', plugin.PAGE)
        self.assertIn("'printer_mods'", plugin.PAGE)
        self.assertIn("имеет приоритет над названием пресета", plugin.LOGIC_PROMPT)
        self.assertEqual(plugin.DEFAULT_CONFIG["printer_mods"], "")

    def test_settings_dialog_separates_printer_and_api_tabs(self):
        self.assertIn('id="settings">Настройки</button>', plugin.PAGE)
        self.assertIn('id="printerTab"', plugin.PAGE)
        self.assertIn('id="apiTab"', plugin.PAGE)
        self.assertIn('aria-labelledby="apiTab" hidden', plugin.PAGE)
        self.assertIn("function setSettingsTab(name)", plugin.PAGE)
        printer_panel = plugin.PAGE.split('id="printerSettings"', 1)[1].split('id="apiSettings"', 1)[0]
        for field in ("printer_mods", "auto_apply", "monitor_url", "klipper_url", "loadKlipper"):
            self.assertIn('id="' + field + '"', printer_panel)
        self.assertIn("type:'klipper_refresh'", plugin.PAGE)
        api_panel = plugin.PAGE.split('id="apiSettings"', 1)[1]
        self.assertIn('id="textFields"', api_panel)
        self.assertIn('id="forget"', api_panel)
        self.assertIn('id="closeSettings" aria-label="Закрыть настройки"', plugin.PAGE)
        self.assertIn("getElementById('closeSettings').onclick=()=>dialog.close()", plugin.PAGE)

    def test_klipper_fetch_filters_configuration_and_state(self):
        class KlipperResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def geturl(self):
                return "http://printer.local:7125/printer/objects/query"

            def read(self, limit):
                return json.dumps({"status": {
                    "configfile": {"config": {
                        "printer": {"max_velocity": "300", "max_accel": "2500"},
                        "extruder": {"pressure_advance": "0.04", "heater_pin": "PE5",
                                     "sensor_type": "Generic 3950"},
                        "mcu": {"serial": "/dev/serial/by-id/private"},
                    }},
                    "gcode_move": {"speed_factor": 1.0, "extrude_factor": 0.98,
                                   "position": [1, 2, 3, 4]},
                    "toolhead": {"max_accel": 2500},
                }}).encode()

        with mock.patch.object(plugin.urllib.request, "urlopen", return_value=KlipperResponse()) as opened:
            context = plugin.fetch_klipper_context("http://printer.local:7125/")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://printer.local:7125/printer/objects/query")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("configfile", json.loads(request.data.decode())["objects"])
        self.assertEqual(context["config"]["extruder"]["pressure_advance"], "0.04")
        self.assertNotIn("heater_pin", context["config"]["extruder"])
        self.assertNotIn("mcu", context["config"])
        self.assertEqual(context["state"]["gcode_move"]["extrude_factor"], 0.98)
        self.assertNotIn("position", context["state"]["gcode_move"])
        with self.assertRaises(ValueError):
            plugin.validate_klipper_url("http://user:secret@printer.local:7125")
        self.assertNotIn("/printer/gcode/script", PLUGIN_PATH.read_text(encoding="utf-8"))

    def test_klipper_refresh_saves_url_and_public_summary(self):
        page = plugin.AIAssistantPage()
        PagesBase.__init__(page)
        snapshot = {"config": {"extruder": {"pressure_advance": "0.04"}},
                    "state": {"gcode_move": {"speed_factor": 1}}}
        with mock.patch.object(plugin, "fetch_klipper_context", return_value=snapshot):
            page._run_klipper_refresh("http://printer.local:7125/")
        self.assertEqual(json.loads(page._stored_config)["klipper_url"],
                         "http://printer.local:7125")
        self.assertEqual(page._klipper_context, snapshot)
        self.assertTrue(page._public_config()["klipper_loaded"])
        self.assertIn("Klipper загружен", page._public_config()["klipper_summary"])
        self.assertTrue(page.outbox[-1]["success"])

    def test_klipper_context_reaches_recommendations_and_gcode_review(self):
        snapshot = {"config": {"extruder": {"pressure_advance": "0.04"}},
                    "state": {"gcode_move": {"speed_factor": 1}}}
        context = plugin.collect_context("", snapshot)
        self.assertEqual(context["klipper"], snapshot)
        with mock.patch.object(plugin, "request_chat",
                               return_value=json.dumps({"setting_changes": []})) as request:
            plugin.extract_recommended_changes(config(), {"answer": "Проверьте PA"}, context)
        self.assertIn("pressure_advance", request.call_args.args[1][-1]["content"])
        with mock.patch.object(plugin, "request_chat", return_value=result()) as request:
            plugin.run_gcode_review(config(), {"lines": 10}, None, snapshot)
        self.assertIn("pressure_advance", request.call_args.args[1][-1]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
