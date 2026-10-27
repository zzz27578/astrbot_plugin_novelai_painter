"""Tools scoped to one Agent request; never mutate registered/shared tools."""
from __future__ import annotations

from astrbot.core.agent.tool import FunctionTool, ToolSet


def isolate_tools(request):
    tools = getattr(request, "func_tool", None)
    if tools is not None:
        request.func_tool = ToolSet(list(tools.func_list))
    return request.func_tool if tools is not None else None


def retire_drawing(*tool_sets):
    for tools in tool_sets:
        if tools is not None:
            tools.remove_tool("novelai_generate_image")


def guard_delivery(event, *tool_sets):
    """The painter already sends this turn's image; retain other media tools.

    Built-in send_message_to_user overrides call(), so wrap that method with
    its original ContextWrapper instead of changing its shared handler.
    """
    for tools in tool_sets:
        if tools is None:
            continue
        original = tools.get_tool("send_message_to_user")
        if original is None or original.handler is not None or isinstance(original, DeliveryGuard):
            continue
        # Bind per instance; separate sessions can safely share the original.
        guarded = DeliveryGuard(name=original.name,
            description=original.description + " 本轮插件生成的图片由绘图插件直接发送，不要填写占位图片地址或重复发送。",
            parameters=original.parameters, active=original.active)
        guarded.delegate = original
        guarded.event = event
        tools.add_tool(guarded)


class DeliveryGuard(FunctionTool):
    async def call(self, context, **kwargs):
        event = self.event
        owned = event.get_extra("nai_image_status") in {"running", "completed", "failed", "cancelled"} or event.get_extra("nai_planner_pending", False)
        session = kwargs.get("session")
        current = not session or session == event.unified_msg_origin
        messages = kwargs.get("messages")
        if owned and current and isinstance(messages, list) and any(
                isinstance(item, dict) and item.get("type") == "image" for item in messages):
            remaining = [item for item in messages if not isinstance(item, dict) or item.get("type") != "image"]
            # An image + caption is one attempted image delivery. Let the Agent
            # write its ordinary reply once instead of sending that caption too.
            if any(isinstance(item, dict) and item.get("type") not in {"plain", "mention_user"} for item in remaining):
                return await self.delegate.call(context, **{**kwargs, "messages": remaining})
            return ("IMAGE_DELIVERY_HANDLED: 本条图片由绘图插件负责发送，已拦截重复图片发送；"
                    f"status={event.get_extra('nai_image_status', 'queued')}。不要编造图片 URL，不要再次调用绘图或发图工具，直接正常回复。")
        return await self.delegate.call(context, **kwargs)
