"""Regressions for the 2026-10-30 JSON-mode failures (neutral scene text)."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import jsonschema
from openai import AsyncOpenAI
from astrbot.api.provider import LLMResponse

import drawing_session
import planner
import test_core as core
import test_planner as planning
import test_planner_http as transport


def shared_materials(kwargs):
    prompt = kwargs["prompt"]
    return json.loads(prompt.split(planner.MATERIALS_HEADER, 1)[1].split("\n" + planner.MATERIALS_END, 1)[0])


class PlannerJSONContractTests(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    make_planner = planning.PlannerTests.make_planner
    queue_marker = planning.PlannerTests.queue_marker
    provider = transport.PlannerHTTPTests.provider

    def shared_planner(self):
        p = self.make_planner({"prompt": "full body, window", "subjects": [{"source": "self"}]})
        p.config.update(drawing_llm_context_mode="shared", drawing_llm_output_mode="json")
        return p

    def snapshot_event(self, message="/n 画窗边的你"):
        event = core.FakeEvent(message)
        event.set_extra("nai_session_snapshot", drawing_session.capture([
            {"role": "user", "content": "We are in the library"},
            {"role": "assistant", "content": "I am reading by a window"}], "Exact persona prefix"))
        return event

    def test_json_instructions_and_native_tool_share_a_fresh_schema(self):
        contract = planner.json_contract()
        schema = json.loads(next(line for line in contract.splitlines() if line.startswith("{")))
        native = planner.plan_tools().openai_schema()[0]["function"]["parameters"]
        self.assertEqual(schema, native)
        self.assertFalse(schema["additionalProperties"])
        jsonschema.validate({"prompt": "window", "subjects": [{"source": "self"}]}, schema)
        for invalid in (
                {"prompt": "window", "subjects": ["1girl", "smiling"]},
                {"prompt": "window", "subjects": [{"source": "self", "self_card_id": "p1"}]},
                {"prompt": "window", "subjects": [], "style": None},
                {"prompt": "window", "subjects": [{"source": "card"}]},
                {"prompt": "window", "subjects": [], "card_mode": "context"}):
            with self.subTest(invalid=invalid), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, schema)
        schema["properties"].clear()
        self.assertIn("subjects", planner.plan_schema()["properties"])

    def test_null_style_preserves_card_style_and_needs_no_model_retry(self):
        async def run():
            p = self.shared_planner()
            p.presets[0]["style_prompt"] = "{atelier pontdarc}, soft lineart"
            p.presets[0]["identity_prompt"] = "{{elf (isekai ojisan)}}, blonde hair, green eyes"
            p.presets[0]["default_outfit_prompt"] = "black bodysuit, yellow split skirt, red boots"
            p.presets[0]["layers_version"] = 1
            raw = {"prompt": "standing by a window", "subjects": [{"source": "self"}], "style": None}
            p.context.llm_generate.return_value = LLMResponse("assistant", completion_text=json.dumps(raw))
            event = self.snapshot_event()
            await p._quick_draw(event, "画窗边的你")
            await core.drain_drawing_tasks(p)
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_awaited_once()
            branch = p._finish_event.await_args.args[0]
            self.assertNotIn("style", branch.get_extra("nai_planner_plan"))
            self.assertIn("{atelier pontdarc}", p._call_official.await_args.args[0])
            self.assertIn("{{elf (isekai ojisan)}}", p._call_official.await_args.args[0])
            self.assertEqual(json.loads(p.context.llm_generate.return_value.completion_text), raw)
        asyncio.run(run())

    def test_matching_subject_alias_is_verified_for_self_and_not_for_other_sources(self):
        materials = {"self_card_id": "p1"}
        raw = {"prompt": "window", "subjects": [{"source": "self", "self_card_id": "p1"}], "style": None}
        repairs = []
        parsed = planner.parse_plan(json.dumps(raw), materials=materials, repairs=repairs)
        self.assertEqual(parsed["subjects"], [{"source": "self", "state_scope": "image"}])
        self.assertEqual(len(repairs), 2)
        self.assertIn("self_card_id", raw["subjects"][0])
        for subject, values in (
                ({"source": "self", "self_card_id": "other"}, materials),
                ({"source": "card", "self_card_id": "p1"}, materials),
                ({"source": "custom", "self_card_id": "p1"}, materials),
                ({"source": "self", "self_card_id": "p1", "preset_id": "other"}, materials),
                ({"source": "self", "self_card_id": "p1"}, {"self_card_id": None})):
            with self.subTest(subject=subject, values=values), self.assertRaises(planner.PlanError) as raised:
                planner.parse_plan(json.dumps({"prompt": "window", "subjects": [subject]}), materials=values)
            self.assertIn("$.subjects[0].self_card_id", [i["path"] for i in raised.exception.issues])

    def test_reference_echo_only_removed_when_equal_to_this_request_and_never_mutates_inputs(self):
        materials = {"card_mode": "context", "self_card_id": "p1", "previous_drawing": {
            "prompt": "previous scene", "subjects": [{"source": "self", "outfit": "white dress"}]},
            "previous_user_request": "draw previous"}
        raw = {"prompt": "new scene", "subjects": [{"source": "self", "outfit": "blue dress"}], **copy.deepcopy(materials)}
        original, original_materials = copy.deepcopy(raw), copy.deepcopy(materials)
        parsed = planner.parse_plan(json.dumps(raw), materials=materials)
        self.assertEqual(set(parsed), {"prompt", "subjects"})
        self.assertEqual(parsed["subjects"][0]["outfit"], "blue dress")
        self.assertEqual(raw, original)
        self.assertEqual(materials, original_materials)
        for changes, inputs in (({"card_mode": "strict"}, materials),
                ({"previous_drawing": {"prompt": "changed"}}, materials),
                ({"unexpected_edit": "erase"}, materials), ({}, None)):
            with self.subTest(changes=changes, inputs=inputs), self.assertRaises(planner.PlanError):
                planner.parse_plan(json.dumps({**raw, **changes}), materials=inputs)
        # Do not consider JSON true interchangeable with JSON 1.
        with self.assertRaises(planner.PlanError):
            planner.parse_plan('{"prompt":"room","subjects":[],"images":{"current_user_images":true}}',
                materials={"images": {"current_user_images": 1}})

    def test_all_structural_errors_are_reported_together_without_guessing_tag_array(self):
        raw = {"prompt": "window", "subjects": ["1girl", "solo", "smiling"], "style": None, "unknown": "edit"}
        with self.assertRaises(planner.PlanError) as raised:
            planner.parse_plan(json.dumps(raw), repairs=[])
        paths = [i["path"] for i in raised.exception.issues]
        self.assertIn("$.unknown", paths)
        self.assertIn("$.subjects[0]", paths)
        self.assertNotIn("$.style", paths)  # Optional null already normalized.
        raw["subjects"] = [{"source": "self", "self_card_id": "wrong", "outfit": None, "use_reference": "false"}]
        with self.assertRaises(planner.PlanError) as raised:
            planner.parse_plan(json.dumps(raw), materials={"self_card_id": "p1"})
        paths = [i["path"] for i in raised.exception.issues]
        self.assertIn("$.subjects[0].self_card_id", paths)
        self.assertIn("$.subjects[0].outfit", paths)
        self.assertIn("$.subjects[0].use_reference", paths)

    def test_flat_tag_list_gets_one_useful_correction_and_no_image_before_valid_plan(self):
        async def run():
            p = self.shared_planner()
            good = p.context.llm_generate.return_value
            bad = {"prompt": "window", "subjects": ["1girl", "solo", "smiling"], "style": None}
            seen = []
            async def generate(**kwargs):
                p._call_official.assert_not_awaited()
                seen.append(shared_materials(kwargs))
                if len(seen) == 1:
                    return LLMResponse("assistant", completion_text=json.dumps(bad))
                correction = seen[-1]["format_correction"]
                self.assertIn("$.subjects[0]", [i["path"] for i in correction["issues"]])
                self.assertIn('"source":"self"', correction["issues"][0]["message"])
                self.assertIn("subjects", correction["instruction"])
                self.assertEqual(json.loads(correction["previous_output"]), bad)
                return good
            p.context.llm_generate.side_effect = generate
            event = self.snapshot_event()
            await p._quick_draw(event, "画窗边的你")
            await core.drain_drawing_tasks(p)
            self.assertEqual(p.context.llm_generate.await_count, 2)
            p._call_official.assert_awaited_once()
            row = p._planner_diagnostics[event.unified_msg_origin]["snapshots"]["response"]
            self.assertEqual(row["format_retry_count"], 1)
            self.assertGreaterEqual(row["provider_total_ms"], row["provider_ms"])
        asyncio.run(run())

    def test_unfixable_tag_list_stops_after_one_correction_without_guessing_self(self):
        async def run():
            p = self.shared_planner()
            p.context.llm_generate.return_value = LLMResponse("assistant",
                completion_text='{"prompt":"2girls, park","subjects":["1girl","other girl"],"style":null}')
            p._notify = AsyncMock()
            await p._quick_draw(self.snapshot_event(), "画两个人")
            await core.drain_drawing_tasks(p)
            self.assertEqual(p.context.llm_generate.await_count, 2)
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
        asyncio.run(run())

    def test_native_tool_returns_have_same_safe_normalization_and_refusals_still_stop(self):
        args = {"prompt": "window", "subjects": [{"source": "self", "self_card_id": "p1"}], "style": None}
        response = LLMResponse("tool", tools_call_name=["novelai_generate_image"], tools_call_args=[args])
        parsed = planner.response_plan(response, materials={"self_card_id": "p1"})
        self.assertNotIn("style", parsed)
        self.assertIn("self_card_id", response.tools_call_args[0]["subjects"][0])
        for text in ('{"error":"cannot process","style":null}', '{"prompt":"partial","subjects":[],"error":"cannot process"}'):
            with self.assertRaises(planner.PlanError) as raised:
                planner.parse_plan(text)
            self.assertEqual(raised.exception.code, "refusal")
            self.assertFalse(raised.exception.retryable)

    def test_n_and_slash_n_accept_logged_echo_shapes_in_one_provider_call(self):
        async def run(marker):
            p = self.shared_planner()
            event = self.snapshot_event("画窗边的你n" if marker else "/n 画窗边的你")
            plan = {"prompt": "full body, window", "subjects": [{"source": "self"}]}
            p._previous_drawing = Mock(return_value={"plan": plan, "user_request": "previous request"})
            async def generate(**kwargs):
                materials = shared_materials(kwargs)
                self.assertIn(planner.MATERIALS_END, kwargs["prompt"])
                self.assertTrue(kwargs["prompt"].endswith(planner.OUTPUT_REMINDER))
                self.assertIn(planner.json_contract(), kwargs["system_prompt"])
                return LLMResponse("assistant", completion_text=json.dumps({"prompt": "full body, window",
                    "subjects": [{"source": "self", "self_card_id": materials["self_card_id"]}], "style": None,
                    **{key: materials[key] for key in ("self_card_id", "card_mode", "previous_drawing", "previous_user_request")}}))
            p.context.llm_generate.side_effect = generate
            with patch("main.logger.info") as logs:
                if marker:
                    await self.queue_marker(p, event)
                    await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text="I read by a window"))
                    await p.drawing_after_reply(event)
                    await core.drain_drawing_tasks(p)
                else:
                    await p._quick_draw(event, "画窗边的你")
                    await core.drain_drawing_tasks(p)
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_awaited_once()
            records = [json.loads(call.args[0].split(": ", 1)[1]) for call in logs.call_args_list if ": {" in call.args[0]]
            normalized = next(d for d in records if d["phase"] == "plan_normalized")
            self.assertFalse(normalized["creates_model_request"])
            self.assertFalse(normalized["changes_scene"])
            self.assertEqual(len(normalized["normalizations"]), 6)
            self.assertEqual(next(d for d in records if d["phase"] == "response")["format_retry_count"], 0)
        asyncio.run(run(False))
        asyncio.run(run(True))

    def test_real_json_wire_keeps_compatibility_format_and_includes_full_contract_once(self):
        async def run(shared):
            bodies = []
            def handle(request):
                body = json.loads(request.content)
                bodies.append(body)
                self.assertEqual(body["response_format"], {"type": "json_object"})
                system = body["messages"][0]["content"]
                self.assertEqual(system.count("[Drawing JSON output contract]"), 1)
                self.assertIn('"additionalProperties":false', system)
                self.assertIn('"maxItems":6', system)
                if shared:
                    self.assertIn(planner.MATERIALS_HEADER, body["messages"][-1]["content"])
                return httpx.Response(200, json={"id": "mock", "model": "grok-4.7", "created": 0,
                    "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"prompt":"window","subjects":[{"source":"self","self_card_id":"p1"}],"style":null}'}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http, max_retries=2)
                provider = self.provider(client)
                original = copy.deepcopy(provider.provider_config)
                p = self.shared_planner()
                if not shared:
                    p.config["drawing_llm_context_mode"] = "legacy"
                p.config["drawing_llm_transport"] = "nonstream"
                p.context.get_provider_by_id = Mock(return_value=provider)
                event = self.snapshot_event()
                await p._quick_draw(event, "画窗边的你")
                await core.drain_drawing_tasks(p)
                self.assertEqual(len(bodies), 1)
                p._call_official.assert_awaited_once()
                self.assertEqual(provider.provider_config, original)
                self.assertEqual(client.max_retries, 2)
                row = p._planner_diagnostics[event.unified_msg_origin]["snapshots"]["request"]
                self.assertGreater(row["json_contract_size"]["chars"], 100)
                self.assertEqual(row["provider_total_ms"], 0)
        asyncio.run(run(False))
        asyncio.run(run(True))
