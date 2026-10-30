"""Concurrency regressions: a new chat turn must not invalidate queued images."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock

from astrbot.api.provider import LLMResponse

import drawing
import drawing_queue
import drawing_session
import test_core as core
import test_planner as planning


class DrawingQueueTests(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    make_planner = planning.PlannerTests.make_planner
    drawing_event = core.CoreTests.drawing_event

    async def submit(self, plugin, text, identifier, *, marker=False, origin=None):
        event = self.drawing_event(text + " n" if marker else "/n " + text, identifier)
        if origin:
            event.unified_msg_origin = origin
        if marker:
            await plugin.drawing_marker_event(event)
            await plugin.drawing_capture_reply(event, LLMResponse("assistant", completion_text="My own reply: " + text))
            event.set_extra("nai_session_snapshot", drawing_session.capture([
                {"role": "user", "content": text},
                {"role": "assistant", "content": "My own reply: " + text}], "Exact persona"))
            await plugin.drawing_after_reply(event)
        else:
            if plugin._shared_drawing_context():
                event.set_extra("nai_session_snapshot", drawing_session.capture([
                    {"role": "user", "content": "Own scene: " + text}], "Exact persona"))
            await plugin._quick_draw(event, text)
        return event

    def test_continued_chat_and_history_rewrite_preserve_inflight_task_and_own_outfit(self):
        async def run(shared, rewrite):
            p = self.make_planner({"prompt": "window", "subjects": [{"source": "self"}]})
            p.config["drawing_llm_context_mode"] = "shared" if shared else "legacy"
            manager = p.context.conversation_manager
            initial = await p._story_view(core.FakeEvent("old scene"))
            p._ledger().remember(initial, drawing.card_layers(p.presets[0]), "blue dress, sandals")
            manager.get_conversation.return_value.history = json.dumps([
                {"role": "user", "content": "old scene"}, {"role": "assistant", "content": "old reply"}])
            started, release = asyncio.Event(), asyncio.Event()
            good = p.context.llm_generate.return_value
            calls = []
            async def generate(**kwargs):
                calls.append(copy.deepcopy(kwargs))
                started.set()
                await release.wait()
                return good
            p.context.llm_generate.side_effect = generate
            event = await self.submit(p, "画窗边的你", "one", marker=True)
            await asyncio.wait_for(started.wait(), 1)
            # Mimic normal core history persistence, including compression or
            # checkpoints being rewritten, while the drawing HTTP is in flight.
            if rewrite:
                manager.get_conversation.return_value.history = json.dumps([
                    {"role": "user", "content": "summary of earlier turns"},
                    {"role": "assistant", "content": "new conversation reply"}])
            else:
                history = json.loads(manager.get_conversation.return_value.history)
                history.extend([{"role": "user", "content": "new chat"}, {"role": "assistant", "content": "new reply"}])
                manager.get_conversation.return_value.history = json.dumps(history)
            newer = await p._story_view(self.drawing_event("new chat", "two"))
            if rewrite:
                self.assertNotEqual(initial.epoch, newer.epoch)  # Reproduces the old cancellation condition.
            p._ledger().remember(newer, drawing.card_layers(p.presets[0]), "red coat, boots")
            release.set()
            await core.drain_drawing_tasks(p)
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_awaited_once()
            prompt = p._call_official.await_args.args[0]
            self.assertIn("blue dress", prompt)
            self.assertNotIn("red coat", prompt)
            self.assertNotIn("new conversation reply", json.dumps(calls))
            self.assertTrue(p._finish_event.await_args.args[1].ok)
            self.assertFalse(p._drawing_queue.lanes)
            self.assertNotIn("nai_planner_plan", event.extras)
        for shared in (True, False):
            for rewrite in (True, False):
                with self.subTest(shared=shared, rewrite=rewrite):
                    asyncio.run(run(shared, rewrite))

    def test_mixed_n_and_slash_n_are_fifo_through_delivery_and_release_chat_lock(self):
        from astrbot.core.utils.session_lock import SessionLockManager
        async def run():
            p = self.make_planner({"prompt": "window", "subjects": []})
            started, release, delivered, allow_delivery = (asyncio.Event() for _ in range(4))
            requests, finished = [], []
            good = p.context.llm_generate.return_value
            async def generate(**kwargs):
                requests.append(core.planning_payload(kwargs)["drawing_request"])
                if len(requests) == 1:
                    started.set()
                    await release.wait()
                return good
            async def deliver(event, result):
                if not finished:
                    delivered.set()
                    await allow_delivery.wait()
                finished.append(event.message_obj.message_id)
                return "done"
            p.context.llm_generate.side_effect = generate
            p._finish_event.side_effect = deliver
            locks = SessionLockManager()
            origin = core.FakeEvent.unified_msg_origin
            async with locks.acquire_lock(origin):
                await self.submit(p, "first scene", "first", marker=True)
            await asyncio.wait_for(started.wait(), 1)
            # /n must also release AstrBot's lock before planning completes.
            async with locks.acquire_lock(origin):
                await self.submit(p, "second scene", "second")
            async with locks.acquire_lock(origin):
                await self.submit(p, "third scene", "third", marker=True)
            self.assertEqual(requests, ["first scene"])
            self.assertEqual(len(p._planner_tasks), 3)
            release.set()
            await asyncio.wait_for(delivered.wait(), 1)
            self.assertEqual(requests, ["first scene"])  # Keep FIFO until image delivery completes.
            allow_delivery.set()
            await core.drain_drawing_tasks(p)
            self.assertEqual(requests, ["first scene", "second scene", "third scene"])
            self.assertEqual(finished, ["first", "second", "third"])
            self.assertEqual(p._call_official.await_count, 3)
            self.assertFalse(p._drawing_queue.lanes)
        asyncio.run(run())

    def test_other_conversations_can_plan_while_one_conversation_waits(self):
        async def run():
            p = self.make_planner({"prompt": "landscape", "subjects": []})
            started, release, other_started = (asyncio.Event() for _ in range(3))
            good = p.context.llm_generate.return_value
            async def generate(**kwargs):
                if core.planning_payload(kwargs)["drawing_request"] == "first":
                    started.set()
                    await release.wait()
                else:
                    other_started.set()
                return good
            p.context.llm_generate.side_effect = generate
            await self.submit(p, "first", "one")
            await asyncio.wait_for(started.wait(), 1)
            await self.submit(p, "other", "two", origin="test:private:2")
            await asyncio.wait_for(other_started.wait(), 1)
            release.set()
            await core.drain_drawing_tasks(p)
            self.assertEqual(p._call_official.await_count, 2)
        asyncio.run(run())

    def test_reset_cancels_running_and_waiting_tasks_then_new_request_can_run(self):
        async def run():
            p = self.make_planner({"prompt": "window", "subjects": []})
            started, release = asyncio.Event(), asyncio.Event()
            good = p.context.llm_generate.return_value
            async def generate(**kwargs):
                started.set()
                await release.wait()
                return good
            p.context.llm_generate.side_effect = generate
            await self.submit(p, "old first", "first")
            await asyncio.wait_for(started.wait(), 1)
            await self.submit(p, "old second", "second", marker=True)
            reset = core.FakeEvent()
            reset.set_extra("_clean_group_context_session", True)
            await p.drawing_context_lifecycle(reset)
            await core.drain_drawing_tasks(p)
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_not_awaited()
            self.assertFalse(p._drawing_queue.lanes)
            release.set()
            await self.submit(p, "new scene", "new")
            await core.drain_drawing_tasks(p)
            p._call_official.assert_awaited_once()
            self.assertFalse(p._drawing_queue.lanes)
        asyncio.run(run())

    def test_actual_conversation_switch_still_rejects_completed_plan(self):
        async def run():
            p = self.make_planner()
            p._notify = AsyncMock()
            good = p.context.llm_generate.return_value
            async def generate(**kwargs):
                p.context.conversation_manager.get_curr_conversation_id.return_value = "other-cid"
                return good
            p.context.llm_generate.side_effect = generate
            await self.submit(p, "old scene", "switch")
            await core.drain_drawing_tasks(p)
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
            self.assertIn("切换", p._notify.await_args.args[1])
            self.assertFalse(p._drawing_queue.lanes)
        asyncio.run(run())

    def test_first_marker_binds_conversation_and_persona_after_core_creates_it(self):
        async def run():
            p = self.make_planner({"prompt": "window", "subjects": [{"source": "self"}]})
            manager = p.context.conversation_manager
            manager.get_curr_conversation_id.return_value = None
            event = self.drawing_event("画你 n", "first")
            await p.drawing_marker_event(event)
            self.assertIsNone(event.get_extra("nai_task_context")["cid"])
            self.assertIsNone(event.get_extra("nai_task_self_card_id"))
            manager.get_curr_conversation_id.return_value = "newly-created"
            p.presets.append({"id": "p2", "name": "New persona", "positive_prompt": "red hair"})
            p.config["default_preset_id"] = "p2"
            await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text="own first reply"))
            await p.drawing_after_reply(event)
            await core.drain_drawing_tasks(p)
            self.assertEqual(event.get_extra("nai_task_context")["cid"], "newly-created")
            self.assertIn("red hair", p._call_official.await_args.args[0])
            self.assertNotIn("blonde hair", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_failed_task_releases_queue_and_does_not_discard_next_request(self):
        async def run():
            p = self.make_planner()
            p.config["drawing_llm_format_retry"] = False
            good = p.context.llm_generate.return_value
            p.context.llm_generate.side_effect = [LLMResponse("assistant", completion_text="broken"), good]
            await self.submit(p, "first", "first")
            await self.submit(p, "second", "second")
            await core.drain_drawing_tasks(p)
            self.assertEqual(p.context.llm_generate.await_count, 2)
            p._call_official.assert_awaited_once()
            self.assertFalse(p._drawing_queue.lanes)
        asyncio.run(run())

    def test_binding_changed_by_later_chat_does_not_retarget_accepted_self(self):
        async def run():
            p = self.make_planner({"prompt": "window", "subjects": [{"source": "self"}]})
            good = p.context.llm_generate.return_value
            async def generate(**kwargs):
                p.presets.append({"id": "p2", "name": "Other", "positive_prompt": "red hair"})
                p.config["default_preset_id"] = "p2"
                return good
            p.context.llm_generate.side_effect = generate
            await self.submit(p, "draw yourself", "bound")
            await core.drain_drawing_tasks(p)
            self.assertIn("blonde hair", p._call_official.await_args.args[0])
            self.assertNotIn("red hair", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_history_rewrite_during_image_api_still_delivers_image(self):
        async def run():
            p = self.make_planner({"prompt": "window", "subjects": [{"source": "self"}]})
            p.context.conversation_manager.get_conversation.return_value.history = json.dumps([
                {"role": "user", "content": "earlier"}, {"role": "assistant", "content": "earlier reply"}])
            image = p.temp_dir / "result.png"
            image.write_bytes(b"mock image")
            del p._finish_event
            p._send_generated_image = AsyncMock()
            async def image_api(*args, **kwargs):
                p.context.conversation_manager.get_conversation.return_value.history = json.dumps([
                    {"role": "user", "content": "compressed newer chat"}])
                await p._story_view(self.drawing_event("continued chat", "next"))
                return str(image)
            p._call_official.side_effect = image_api
            await self.submit(p, "画窗边的你", "image")
            await core.drain_drawing_tasks(p)
            p._call_official.assert_awaited_once()
            p._send_generated_image.assert_awaited_once()
            self.assertTrue(p._send_generated_image.await_args.args[1].ok)
        asyncio.run(run())

    def test_empty_history_clear_without_lifecycle_hook_still_rejects_old_plan(self):
        async def run():
            p = self.make_planner()
            p.context.conversation_manager.get_conversation.return_value.history = json.dumps([
                {"role": "user", "content": "earlier"}])
            good = p.context.llm_generate.return_value
            async def generate(**kwargs):
                p.context.conversation_manager.get_conversation.return_value.history = "[]"
                return good
            p.context.llm_generate.side_effect = generate
            p._notify = AsyncMock()
            await self.submit(p, "old scene", "cleared")
            await core.drain_drawing_tasks(p)
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
            self.assertFalse(p._drawing_queue.lanes)
        asyncio.run(run())

    def test_queue_timeout_is_reported_and_does_not_send_model_or_image_request(self):
        async def run():
            p = self.make_planner()
            p._drawing_queue = drawing_queue.DrawingQueue()
            p._drawing_queue.wait = AsyncMock(side_effect=asyncio.TimeoutError())
            p._notify = AsyncMock()
            event = await self.submit(p, "wait", "timeout")
            await core.drain_drawing_tasks(p)
            p.context.llm_generate.assert_not_awaited()
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
            detail = p._planner_diagnostics[event.unified_msg_origin]["snapshots"]["failed"]
            self.assertEqual(detail["status"], "queue_timeout")
            self.assertFalse(p._drawing_queue.lanes)
        asyncio.run(run())

    def test_ticket_cancellation_timeout_and_cleanup_are_independent(self):
        async def run():
            queue = drawing_queue.DrawingQueue()
            first, second, third = (queue.reserve("same") for _ in range(3))
            with self.assertRaises(asyncio.TimeoutError):
                await queue.wait(second, .01)
            queue.release(second)
            self.assertFalse(third.ready.done())
            queue.release(first)
            queue.release(first)
            await queue.wait(third, 1)
            queue.release(third)
            self.assertFalse(queue.lanes)
        asyncio.run(run())
