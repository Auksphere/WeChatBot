"""One administrator private-chat command to sync and download added sticker packs."""

import asyncio

from astrbot.api import logger

from .adapter import track_send_task
from .native_client import NativeAPIError, NativeClient
from .state import clean_sticker_caption
from .tool_support import context, is_native_event, is_tool_error, result

JOB_WAIT_SECONDS = 300
POLL_INTERVAL_SECONDS = 2


def valid_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value.encode()) <= 1024
        and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
    )


async def added_packs(adapter):
    page = await adapter.client.sticker_packs()
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise ValueError("invalid_sticker_pack_list")
    return list(
        {
            p["id"]: p
            for p in page["items"]
            if isinstance(p, dict) and p.get("membership") == "added" and valid_id(p.get("id"))
        }.values()
    )


async def inspect_library_operation(adapter, row, job=None):
    action, key = row["action"], row["idempotency_key"]
    state, job_id = row["state"], row["job_id"]
    if state in {"success", "failed"}:
        return result(action, state, job_id=job_id)
    state = "needs_review"
    try:
        if job is None:
            if not job_id:
                return result(
                    action, "pending" if row["state"] == "submitting" else state, job_id=None
                )
            job = await adapter.client.job(job_id)
        expected_kind = "sticker_pack" if action == "library:sync" else "sticker_acquire"
        if (
            not isinstance(job, dict)
            or job.get("accountId") != row["account"]
            or job.get("kind") != expected_kind
            or not valid_id(job.get("id"))
            or (job_id and job["id"] != job_id)
        ):
            raise ValueError("invalid_sticker_library_receipt")
        job_id = job["id"]
        payload = job.get("result")
        payload = payload if isinstance(payload, dict) else {}
        if job.get("state") in {"queued", "running"}:
            state = "pending"
        elif job.get("state") == "available" and job.get("phase") == "completed":
            complete = (
                payload.get("serverAcceptance") in {"accepted", "reconciled_noop"}
                and payload.get("profileWriteback") == "complete"
            )
            if action.startswith("library:download"):
                complete = complete and (
                    payload.get("packId") == row["content_id"]
                    and payload.get("membership") == "added"
                    and payload.get("contentReadiness") == "available"
                )
            if complete:
                state = "success"
        elif job.get("state") in {"failed", "cancelled", "expired", "unsupported"}:
            state = "failed"
    except (NativeAPIError, ValueError, TypeError):
        state = "needs_review"
    adapter.state.finish_sticker(key, state, job_id)
    return result(action, state, job_id=job_id)


def operation_text(outcome):
    label = "同步" if outcome["action"] == "library:sync" else "下载"
    text = {
        "pending": f"{label}任务仍在处理中，尚未完成。",
        "failed": f"{label}任务失败，请检查 Native 任务与连接状态。",
        "needs_review": f"{label}结果待核对，请勿重复提交或更换任务重试。",
    }[outcome["status"]]
    job_id = outcome.get("job_id")
    if job_id:
        text += f"\n任务：{job_id}"
    return text + "\n稍后可再次发送 /表情包同步，插件会核对原在途任务。"


def management_context(event):
    if event.is_admin() is not True or event.is_private_chat() is not True:
        raise ValueError("sticker_management_permission_revoked")
    return context(event, stickers=True)


async def library_operation(event, pack_id=""):
    adapter = management_context(event).adapter
    action = "library:download:" + pack_id if pack_id else "library:sync"
    source = {"packId": pack_id} if pack_id else {"scope": "all"}
    row, won = adapter.stickers.claim(
        event.message_obj.raw_message, action, pack_id or "all", source
    )
    if won:
        try:
            job = (
                await NativeClient.download_sticker_pack(
                    adapter.client, pack_id, row["idempotency_key"]
                )
                if pack_id
                else await NativeClient.sync_stickers(adapter.client, row["idempotency_key"])
            )
            outcome = await inspect_library_operation(adapter, row, job)
        except (Exception, asyncio.CancelledError) as error:
            state = (
                "failed"
                if isinstance(error, NativeAPIError)
                and 400 <= error.status < 500
                and error.status not in {408, 409, 429}
                else "needs_review"
            )
            adapter.state.finish_sticker(row["idempotency_key"], state)
            if isinstance(error, asyncio.CancelledError):
                raise
            outcome = result(row["action"], state, job_id=row["job_id"])
    else:
        outcome = await inspect_library_operation(adapter, row)
    try:
        async with asyncio.timeout(JOB_WAIT_SECONDS):
            while outcome["status"] == "pending" and outcome.get("job_id"):
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                management_context(event)
                row = dict(row, state="pending", job_id=outcome["job_id"])
                outcome = await inspect_library_operation(adapter, row)
    except TimeoutError:
        pass
    management_context(event)
    return outcome


async def sync_library(event):
    adapter = management_context(event).adapter
    outcome = await library_operation(event)
    if outcome["status"] != "success":
        return operation_text(outcome)
    packs = await added_packs(adapter)
    management_context(event)
    if not packs:
        return (
            "表情库目录同步完成，当前没有已添加的官方包。请先在手机微信添加，再发送 /表情包同步。"
        )
    counts = {"success": 0, "ready": 0, "failed": 0, "unfinished": 0}
    issues = []
    for pack in packs:
        management_context(event)
        if pack.get("contentReadiness") == "available":
            counts["ready"] += 1
            continue
        outcome = await library_operation(event, pack["id"])
        status = outcome["status"]
        counts[status if status in {"success", "failed"} else "unfinished"] += 1
        if status != "success":
            label = {"failed": "失败", "pending": "处理中", "needs_review": "待核对"}[status]
            name = clean_sticker_caption(pack.get("name"))[:50] or "未命名"
            job = f" · 任务 {outcome['job_id']}" if outcome.get("job_id") else ""
            issues.append(f"{name}（{pack['id']}）：{label}{job}")
    complete = not counts["failed"] and not counts["unfinished"]
    text = "表情包同步完成。" if complete else "表情包同步结束，部分包未完成。"
    text += (
        f"\n共 {len(packs)} 个包：下载成功 {counts['success']}，已就绪 {counts['ready']}，"
        f"失败 {counts['failed']}，未完成 {counts['unfinished']}。"
    )
    if issues:
        text += "\n" + "\n".join(issues[:10])
        if len(issues) > 10:
            text += f"\n另有 {len(issues) - 10} 个异常包。"
        text += "\n稍后可再次发送 /表情包同步；已就绪的包会跳过，在途任务只核对原回执。"
    else:
        text += "\n现在可以在聊天中请求查找并发送本地可用的 GIF 表情。"
    return text


async def run_sticker_command(event, *, notify=None):
    adapter, started = None, False
    try:
        if (
            not is_native_event(event)
            or event.is_admin() is not True
            or event.is_private_chat() is not True
        ):
            return "此指令仅允许管理员通过 WeChat Native 私聊操作。"
        if not event.adapter.native_config.enable_stickers:
            return "请先在机器人配置中开启 enable_stickers。"
        adapter = management_context(event).adapter
        if getattr(adapter, "_sticker_sync_running", False):
            return "表情包正在同步，完成后会自动汇总结果，无需重复发送。"
        adapter._sticker_sync_running = started = True
        async with track_send_task(adapter):
            if notify is not None:
                await notify("开始同步表情库并自动下载所有已添加的包，完成后会汇总结果，请稍候。")
            return await sync_library(event)
    except Exception as error:
        if is_tool_error(error):
            return "当前暂时无法管理表情库，请检查机器人开关、私聊权限和连接状态。"
        logger.warning("WeChat Native 表情管理指令失败：error_type=%s", type(error).__name__)
        return "表情库操作暂时无法完成，请检查 Native 连接和任务状态。"
    finally:
        if started:
            adapter._sticker_sync_running = False
