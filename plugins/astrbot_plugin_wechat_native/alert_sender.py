"""Send deterministic notifications through AstrBot's public session API."""

import asyncio

from astrbot.api import logger
from astrbot.api.event import MessageChain


def enabled_platform(context, platform_id, platform_type):
    platform = context.get_platform_inst(platform_id)
    if platform is None:
        return False
    meta = platform.meta()
    return (
        meta.id == platform_id
        and meta.name == platform_type
        and platform.config.get("enable") is True
        and any(
            isinstance(p, dict)
            and p.get("id") == platform_id
            and p.get("type") == platform_type
            and p.get("enable") is True
            for p in context.get_config().get("platform", [])
        )
    )


class AlertSender:
    def __init__(self, context):
        self.context = context

    async def send(self, binding, bot_id, kind, failure_kind=""):
        reasons = {"unauthenticated": "微信账号未登录", "connection_offline": "微信连接异常"}
        if kind == "outage":
            text = (
                f"⚠️ WeChat Native 已断线\n机器人：{bot_id}\n"
                f"原因：{reasons.get(failure_kind, 'Native 服务不可用')}\n"
                "检测：连续 3 次异常\n请打开 AstrBot 的 WeChat Native 测试面板检查。"
            )
        elif kind == "recovery":
            text = f"✅ WeChat Native 已恢复\n机器人：{bot_id}\n状态：微信已重新上线"
        else:
            text = (
                f"🔔 WeChat Native 通知测试\n机器人：{bot_id}\n"
                "这是一条测试消息，不代表连接出现故障。"
            )
        return await self._send_text(binding, text)

    async def send_friend(self, binding, request):
        lines = ["📩 收到新的微信好友申请"]
        if name := request.get("displayName"):
            lines.append(f"申请人：{name}")
        if message := request.get("message"):
            lines.append(f"验证消息：{message}")
        lines.extend(("", "可以私聊微信机器人说：", "“查看好友申请”"))
        if name:
            lines.append(f"“通过{name}的申请”")
        return await self._send_text(binding, "\n".join(lines))

    async def send_event_cursor_expired(self, binding, bot_id):
        return await self._send_text(
            binding,
            f"⚠️ WeChat Native {bot_id} 的事件游标已过期，已保留游标并停止接收。"
            "请检查 Native 事件保留历史与插件日志。",
        )

    async def _send_text(self, binding, text):
        try:
            if not enabled_platform(self.context, binding["platform_id"], binding["platform_type"]):
                logger.warning("Native 通知目标平台不可用，跳过发送")
                return "skipped_invalid_platform"
            async with asyncio.timeout(10):
                sent = await self.context.send_message(binding["umo"], MessageChain().message(text))
            if sent is True:
                return "sent"
        except Exception:
            pass
        logger.warning("Native 通知发送尝试失败，不自动重试")
        return "failed"
