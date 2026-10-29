import asyncio
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import AsyncOpenAI
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.tool import ToolSet
from astrbot.core.message.components import Image

import drawing_session as session
import drawing_runtime
import planner
import planner_transport
import test_core as core
import test_planner as planning
import test_planner_http as transport
from main import GenerationResult


def tool_block():
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "old-call", "type": "function",
            "function": {"name": "novelai_generate_image", "arguments": '{"prompt":"old scene","subjects":[]}'}}]},
        {"role": "tool", "tool_call_id": "old-call", "content": "Earlier image delivered"},
    ]


class DrawingSessionTests(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    make_planner = planning.PlannerTests.make_planner
    drawing_event = core.CoreTests.drawing_event
    queue_marker = planning.PlannerTests.queue_marker
    provider = transport.PlannerHTTPTests.provider

    def shared_plugin(self):
        p = self.make_planner({"prompt": "window, reading", "subjects": [{"source": "self"}]})
        p.config["drawing_llm_context_mode"] = "shared"
        p.config["drawing_llm_context_messages"] = 0
        return p

    def cold_context(self, p, history=None):
        cfg = {"provider_settings": {"computer_use_runtime": "none", "max_context_length": 8,
            "llm_safety_mode": False}, "timezone": "Asia/Shanghai"}
        p.context.get_config = Mock(return_value=cfg)
        p.context.persona_manager = SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=(
            "elf-persona", {"prompt": "You are the elf in this chat.", "tools": [], "skills": None,
                "_begin_dialogs_processed": [{"role": "user", "content": "opening scene"},
                    {"role": "assistant", "content": "opening response"}]}, None, False)))
        p.context.subagent_orchestrator = None
        p.context.get_llm_tool_manager = Mock(return_value=SimpleNamespace(get_full_tool_set=lambda: ToolSet()))
        p.context.conversation_manager.get_conversation.return_value = SimpleNamespace(
            cid="conversation-1", persona_id="elf-persona", history=json.dumps(history or []))
        return p.context.conversation_manager

    def test_default_shared_and_zero_does_not_disable_native_history(self):
        self.assertEqual(planner.DEFAULTS["drawing_llm_context_mode"], "shared")
        async def run():
            p = self.shared_plugin()
            e = self.drawing_event("画窗边的你n", "shared-n")
            await self.queue_marker(p, e)
            records = [{"role": "system", "content": "\n Exact persona \n"},
                {"role": "user", "content": "Previous turn"}, *tool_block(),
                {"role": "assistant", "content": "Previous reply"},
                {"role": "user", "content": "画窗边的你\n[NovelAI drawing identities]don't draw[End NovelAI drawing identities]"},
                {"role": "assistant", "content": "我在窗边读书。"}]
            before = copy.deepcopy(records)
            await p.drawing_capture_reply(e, LLMResponse("assistant", completion_text="我在窗边读书。"))
            await p.drawing_agent_done(e, SimpleNamespace(messages=records), None)
            await p.drawing_after_reply(e)
            records[-1]["content"] = "Later chat must not replace the snapshot"
            await core.drain_drawing_tasks(p)
            call = p.context.llm_generate.await_args.kwargs
            self.assertTrue(call["system_prompt"].startswith("\n Exact persona \n"))
            self.assertEqual(call["contexts"][-1]["content"], before[-1]["content"])
            self.assertEqual(call["contexts"][1:3], tool_block())
            self.assertNotIn("don't draw", str(call))
            self.assertEqual(call["chat_provider_id"], "drawing-provider")
            self.assertIn("[NovelAI drawing identities]", records[-2]["content"])
            p._call_official.assert_awaited_once()
            self.assertNotIn("nai_planner_plan", e.extras)  # Branch-local state.
        asyncio.run(run())

    def test_cold_slash_n_uses_real_core_persona_and_skills_without_writes(self):
        async def run():
            p = self.shared_plugin()
            manager = self.cold_context(p, [{"role": "user", "content": "We moved to a library"}])
            manager.new_conversation = AsyncMock()
            manager.update_conversation = AsyncMock()
            original = manager.get_conversation.return_value.history
            event = self.drawing_event("/n 画当前场景", "slash")
            with patch("astrbot.core.astr_main_agent.SkillManager") as skills, \
                    patch("astrbot.core.astr_main_agent.build_skills_prompt", return_value="Core selected skills"):
                skills.return_value.list_skills.return_value = [SimpleNamespace(name="painting", source_type="user")]
                await p._quick_draw(event, "画当前场景")
            call = p.context.llm_generate.await_args.kwargs
            self.assertIn("You are the elf in this chat.", call["system_prompt"])
            self.assertIn("Core selected skills", call["system_prompt"])
            self.assertIn("library", str(call["contexts"]))
            self.assertEqual(call["contexts"][0]["content"], "opening scene")
            self.assertEqual(manager.get_conversation.return_value.history, original)
            manager.new_conversation.assert_not_called()
            manager.update_conversation.assert_not_called()
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_awaited_once()
            self.assertFalse(event.call_llm)
        asyncio.run(run())

    def test_no_conversation_uses_default_persona_without_creating_chat(self):
        async def run():
            p = self.shared_plugin()
            manager = self.cold_context(p)
            manager.get_curr_conversation_id.return_value = None
            with patch("astrbot.core.astr_main_agent.SkillManager") as skills:
                skills.return_value.list_skills.return_value = []
                snapshot = await session.session_snapshot(p.context, core.FakeEvent())
            self.assertIn("elf", snapshot.system)
            manager.get_conversation.assert_not_awaited()
            self.assertIsNone(p.context.persona_manager.resolve_selected_persona.await_args.kwargs["conversation_persona_id"])
        asyncio.run(run())

    def test_trim_whole_turns_and_preserve_tool_pairs_and_pixels(self):
        rows = [{"role": "user", "content": "old" * 800}, *tool_block(),
            {"role": "user", "content": [{"type": "text", "text": "current"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 10000}}]},
            {"role": "assistant", "content": "current reply"}]
        snapshot = session.capture(rows, "persona", images=True)
        parts, _, metrics = session.request_parts(snapshot, {"drawing_request": "draw"}, "submit", 1000)
        self.assertEqual(metrics["trimmed_turns"], 1)
        self.assertEqual(len(parts["contexts"]), 2)
        self.assertIn("data:image", str(parts))
        self.assertEqual(len(snapshot.messages), 5)
        with self.assertRaisesRegex(ValueError, "上限"):
            session.request_parts(session.capture([{ "role": "user", "content": "X" * 3000}]),
                {"drawing_request": "draw"}, "submit", 1000)

    def test_malformed_tools_and_private_fields_never_reach_wire(self):
        rows = [{"role": "tool", "tool_call_id": "orphan", "content": "old"}]
        for identity in (None, [], {}, 3):
            row = tool_block()[0]
            row["tool_calls"][0]["id"] = identity
            row["content"] = "valid text"
            row["reasoning_content"] = "private reasoning"
            rows.append(row)
        rows.append({"role": "assistant", "tool_calls": {"id": "not a list"}, "content": "plain"})
        clean = session.native_messages(rows)
        self.assertEqual(len(clean), 5)
        self.assertNotIn("tool_calls", str(clean))
        self.assertNotIn("private reasoning", str(clean))
        self.assertNotIn("orphan", str(clean))

    def test_input_pinning_survives_original_cleanup_and_leaves_no_files(self):
        from PIL import Image as PillowImage
        async def run():
            p = self.shared_plugin()
            source = p.temp_dir / "input.png"
            PillowImage.new("RGB", (1, 1), "white").save(source)
            pixels = source.read_bytes()
            event = core.FakeEvent()
            component = Image.fromFileSystem(source)
            event.get_messages = lambda: [component]
            event.set_extra("nai_planner_images", [str(source)])
            event.set_extra("nai_previous_drawing", {"image": str(source)})
            event.set_extra("nai_session_snapshot", session.capture([
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": source.as_uri()}}]}], images=True))
            branch = session.BranchEvent(event)
            owned = session.pin_inputs(branch, p.temp_dir)
            source.unlink()
            self.assertEqual(len(owned), 1)
            self.assertEqual(owned[0].read_bytes(), pixels)
            self.assertEqual(Path(await branch.get_messages()[0].convert_to_file_path()), owned[0])
            await session.prepare_native_images(branch.get_extra("nai_session_snapshot"))
            self.assertTrue(session.image_urls(branch.get_extra("nai_session_snapshot"))[0].startswith("data:image/"))
            self.assertEqual(event.get_extra("nai_previous_drawing")["image"], str(source))
            session.cleanup_inputs(owned)
            self.assertFalse(owned[0].exists())
        asyncio.run(run())

    def test_native_images_are_opt_in_bounded_and_not_repeated_for_n(self):
        async def run():
            p = self.shared_plugin()
            rows = [{"role": "user", "content": [
                {"type": "text", "text": "image scene"},
                *[{"type": "image_url", "image_url": {"url": f"https://mock.invalid/{i}.png"}} for i in range(3)]]}]
            self.assertEqual(session.image_urls(session.capture(rows)), [])
            snapshot = session.capture(rows, "persona", images=True)
            self.assertEqual(len(session.image_urls(snapshot)), 2)
            p.config["drawing_llm_use_images"] = True
            event = core.FakeEvent("画风景")
            event.set_extra("nai_session_snapshot", snapshot)
            event.set_extra("nai_planner_images", session.image_urls(snapshot))
            await p._plan_drawing(event, {"prompt": "画风景"})
            call = p.context.llm_generate.await_args.kwargs
            self.assertIsNone(call["image_urls"])
            self.assertEqual(len(session.image_urls(session.capture(call["contexts"], images=True))), 2)
        asyncio.run(run())

    def test_unload_cancels_running_background_and_cleans_owned_files(self):
        async def run():
            p = self.shared_plugin()
            event = self.drawing_event("draw n", "unload")
            await self.queue_marker(p, event)
            await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text="reply"))
            event.set_extra("nai_session_snapshot", session.capture([], "persona"))
            started = asyncio.Event()
            async def wait_forever(*args):
                started.set()
                await asyncio.Future()
            p._plan_drawing = wait_forever
            p._sync_marker_filter = Mock()
            await p.drawing_after_reply(event)
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(p.terminate(), 1)
            self.assertFalse(p._planner_tasks)
            p._call_official.assert_not_awaited()
        asyncio.run(run())

    def test_context_config_roundtrip_and_reject_invalid_mode(self):
        async def run():
            p = self.shared_plugin()
            p._save_config = Mock()
            p._page_error = Mock(side_effect=lambda message: {"error": message})
            values = {"drawing_llm_context_mode": "shared", "drawing_llm_context_chars": 64000,
                "drawing_llm_context_messages": 0}
            with patch("main.request", SimpleNamespace(json=AsyncMock(return_value=values))), \
                    patch("main.json_response", side_effect=lambda value: value):
                self.assertTrue((await p.page_save_config())["saved"])
            self.assertEqual(p.config["drawing_llm_context_chars"], 64000)
            with patch("main.request", SimpleNamespace(json=AsyncMock(return_value={"drawing_llm_context_mode": "unknown"}))):
                self.assertIn("error", await p.page_save_config())
            self.assertEqual(p.config["drawing_llm_context_mode"], "shared")
        asyncio.run(run())

    def test_background_releases_real_session_lock_before_planner_finishes(self):
        from astrbot.core.utils.session_lock import SessionLockManager
        async def run():
            p = self.shared_plugin()
            event = self.drawing_event("draw n", "nonblocking")
            await self.queue_marker(p, event)
            await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text="reply"))
            event.set_extra("nai_session_snapshot", session.capture([{ "role": "assistant", "content": "reply"}], "persona"))
            started, release, acquired = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def plan(*args):
                started.set()
                await release.wait()
                return {"prompt": "room", "subjects": []}
            p._plan_drawing = plan
            p._image_tool_entry = AsyncMock()
            locks = SessionLockManager()
            async def chat_pipeline():
                async with locks.acquire_lock(event.unified_msg_origin):
                    await p.drawing_after_reply(event)
                    event.set_extra("history_saved", True)
            async def next_chat():
                async with locks.acquire_lock(event.unified_msg_origin):
                    acquired.set()
            await asyncio.wait_for(chat_pipeline(), 1)
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(next_chat(), 1)
            self.assertTrue(acquired.is_set())
            self.assertTrue(event.get_extra("history_saved"))
            p._image_tool_entry.assert_not_awaited()
            release.set()
            await core.drain_drawing_tasks(p)
            p._image_tool_entry.assert_awaited_once()
        asyncio.run(run())

    def test_reset_cancels_task_before_start_and_cleans_pinned_files(self):
        async def run():
            p = self.shared_plugin()
            p.config["drawing_llm_use_images"] = True
            event = self.drawing_event("draw n", "cancel")
            await self.queue_marker(p, event)
            await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text="reply"))
            event.set_extra("nai_session_snapshot", session.capture([], "persona"))
            source = p.temp_dir / "source.png"; source.write_bytes(b"image")
            event.set_extra("nai_planner_images", [str(source)])
            await p.drawing_after_reply(event)
            self.assertTrue(list(p.temp_dir.glob("drawing-input-*")))
            reset = core.FakeEvent()
            reset.set_extra("_clean_group_context_session", True)
            await p.drawing_context_lifecycle(reset)
            await core.drain_drawing_tasks(p)
            self.assertFalse(list(p.temp_dir.glob("drawing-input-*")))
            p.context.llm_generate.assert_not_awaited()
            self.assertTrue(source.exists())
        asyncio.run(run())

    def test_shared_request_reaches_real_provider_with_native_tool_history(self):
        async def run():
            bodies = []
            def handle(request):
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "mock", "model": "grok-4.7", "created": 0,
                    "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"prompt":"window","subjects":[]}'}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http, max_retries=2)
                provider = self.provider(client)
                p = self.shared_plugin()
                p.config.update(drawing_llm_output_mode="json", drawing_llm_transport="nonstream")
                p.context.get_provider_by_id = Mock(return_value=provider)
                event = core.FakeEvent("画风景")
                rows = [{"role": "user", "content": "old"}, *tool_block(), {"role": "assistant", "content": "done"}]
                event.set_extra("nai_session_snapshot", session.capture(rows, "Exact main persona"))
                await p._plan_drawing(event, {"prompt": "画风景"})
                self.assertEqual(len(bodies), 1)
                body = bodies[0]
                self.assertEqual(body["model"], "grok-4.7")
                self.assertEqual(body["messages"][2:4], tool_block())
                self.assertTrue(body["messages"][0]["content"].startswith("Exact main persona"))
                self.assertEqual(client.max_retries, 2)
                self.assertEqual(provider.provider_config["custom_extra_body"], {})
                p.context.llm_generate.assert_not_awaited()
        asyncio.run(run())

    def test_main_http_observer_preserves_body_retries_and_shared_provider(self):
        from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
        async def run():
            bodies, logs = [], []
            def handle(request):
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "mock", "model": "grok-4.7", "created": 0,
                    "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "private main answer"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
                        "prompt_tokens_details": {"cached_tokens": 80}}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http, max_retries=2)
                provider = self.provider(client)
                await provider.text_chat(prompt="same input", system_prompt="same persona")
                event = core.FakeEvent()
                run = SimpleNamespace(context=SimpleNamespace(event=event))
                runner = SimpleNamespace(run_context=run, provider=provider)
                with patch.dict(_ACTIVE_AGENT_RUNNERS, {event.unified_msg_origin: runner}):
                    self.assertTrue(drawing_runtime.observe_main(event, run,
                        lambda phase, metrics, pid: logs.append((phase, copy.deepcopy(metrics), pid))))
                    self.assertIsNot(runner.provider, provider)
                    self.assertEqual(runner.provider.client.max_retries, client.max_retries)
                    await runner.provider.text_chat(prompt="same input", system_prompt="same persona")
                    drawing_runtime.restore_observation(event)
                self.assertIs(runner.provider, provider)
                self.assertEqual(bodies[0], bodies[1])
                end = next(row[1] for row in logs if row[0] == "http_end")
                self.assertEqual(end["usage"]["cached_tokens"], 80)
                self.assertNotIn("private main answer", str(logs))
                self.assertFalse(http.is_closed)
        asyncio.run(run())

    def test_older_background_completion_cannot_replace_newer_drawing_memory(self):
        async def run():
            p = self.shared_plugin()
            for seq, description in ((2, "newer"), (1, "older")):
                event = self.drawing_event(description, description)
                event.set_extra("nai_drawing_sequence", seq)
                event.set_extra("nai_generation_view", await p._story_view(event))
                event.set_extra("nai_planner_plan", {"prompt": description, "subjects": []})
                p._remember_planned_drawing(event, GenerationResult(ok=True, job_id="mock", provider="mock", path=""))
            row = next(iter(p._drawing_memory.values()))
            self.assertEqual(row["plan"]["prompt"], "newer")
        asyncio.run(run())
