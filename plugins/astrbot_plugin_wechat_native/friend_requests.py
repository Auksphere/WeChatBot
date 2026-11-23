"""Account-scoped contact discovery, mutations and friend request notifications."""

import asyncio
import hashlib
import json
import logging
import time

from astrbot.api import logger
from astrbot.core.star.star import star_map

from .native_client import NativeAPIError
from .state import cursor_number
from .tool_support import ToolError, context, encode, error_result, page_args, result

FRIEND_TOOLS = {
    "wechat_search_contact",
    "wechat_list_contacts",
    "wechat_send_friend_request",
    "wechat_list_friend_requests",
    "wechat_accept_friend_request",
}
_TOOL_LOG_PREFIXES = tuple(
    prefix for name in FRIEND_TOOLS for prefix in (f"使用工具：{name}，", f"Tool `{name}` Result:")
)
NOT_FOUND = {"not_found", "contact_request_not_found"}
CONTACT_RELATIONS = ("friend", "non_contact", "unknown")
ERROR_MESSAGES = {
    "stale_contact_request": "申请已变化，请重新查询后再操作。",
    "stale_recipient_reference": "搜索引用已失效，请用微信号重新搜索。",
    "contact_request_not_actionable": "这条申请当前无法接受，请重新查询待处理申请。",
    "failed": "好友申请操作未能完成。",
    "configuration_error": "好友申请写操作需要有效的 Native Admin Token。",
}


class FriendToolLogFilter(logging.Filter):
    def filter(self, record):
        # AstrBot 4.29.3 logs tool arguments and results before and after the callback.
        return record.module != "tool_loop_agent_runner" or not record.getMessage().startswith(
            _TOOL_LOG_PREFIXES
        )


def _hash(*values):
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _opaque(value, limit=512):
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and all(33 <= ord(char) <= 126 and char not in "/\\" for char in value)
        and value not in {".", ".."}
    )


def _text(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _incoming(item):
    return (
        item.get("direction") == "incoming"
        and item.get("actionable") is True
        and item.get("isContact") is not True
    )


def _preview(item):
    fields = {
        "requestId": item["id"],
        "displayName": _text(item.get("displayName"), 80),
        "message": _text(item.get("message"), 160),
    }
    if type(item.get("updatedAt")) is int:
        fields["updatedAt"] = item["updatedAt"]
    return {key: value for key, value in fields.items() if value != ""}


def _outcome(action, status, operation_id=None):
    if status in ERROR_MESSAGES:
        output = result(
            action,
            status if status in {"configuration_error", "failed"} else "invalid_request",
            message=ERROR_MESSAGES[status],
            reason=status,
        )
    elif status == "contact":
        output = result(action, "success", message="已成功添加好友。")
    elif status == "submitted":
        output = result(
            action,
            "success",
            message=(
                "申请确认已提交，好友关系待微信同步。"
                if action == "accept_request"
                else "好友申请已提交，等待对方接受。"
            ),
        )
    else:
        output = result(action, status)
    if operation_id:
        output["operationId"] = operation_id
    return output


def friend_bindings():
    # Resolve the current plugin instance even when AstrBot reloads it while
    # retaining a running platform adapter.
    metadata = star_map.get(f"{__package__}.main")
    if metadata and metadata.activated:
        return getattr(metadata.star_cls, "alert_bindings", None)
    return None


class FriendRequests:
    def __init__(self, adapter):
        self.adapter = adapter
        self._references = {}
        self._mutation_lock = asyncio.Lock()

    @property
    def account(self):
        return self.adapter.native_config.account_id

    async def search(self, ctx, query):
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode()) > 512
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in query)
        ):
            raise ToolError("invalid_request")
        query = query.strip()
        try:
            item = await self.adapter.client.search_contact(query)
        except NativeAPIError as error:
            if error.status == 404 or error.code == "contact_discovery_rejected":
                return result(
                    "search_contact",
                    "success",
                    found=False,
                    message="未找到可添加的微信用户，请核对微信号。",
                )
            raise
        if item.get("accountId", ctx.account) != ctx.account:
            raise ToolError("permission_denied")
        name = _text(item.get("displayName"), 80)
        if not name:
            return result("search_contact", "success", found=False, message="未找到微信用户。")
        fields = {"found": True, "displayName": name, "isContact": bool(item.get("contactId"))}
        ref, expiry = item.get("recipientRef"), item.get("expiresAt")
        now = time.time()
        if fields["isContact"]:
            message = "已是好友。"
        elif _opaque(ref, 8192) and type(expiry) is int and expiry > now:
            self._references = {
                key: value for key, value in self._references.items() if value[0] > now
            }
            if len(self._references) >= 128:
                self._references.pop(next(iter(self._references)))
            self._references[ref] = (expiry, _hash(query.casefold()))
            fields.update(recipientRef=ref, expiresAt=expiry)
            message = "找到可申请的微信用户。"
        else:
            message = "找到微信用户，当前无法发送好友申请。"
        return result("search_contact", "success", message=message, **fields)

    async def list_contacts(self, search, relation, cursor):
        limit = 12
        page_args(limit, cursor)
        if relation not in (*CONTACT_RELATIONS, "all"):
            raise ToolError("invalid_request")
        if not isinstance(search, str) or len(search.encode()) > 256:
            raise ToolError("invalid_request")
        page = await self.adapter.client.contacts(
            limit=limit, relation=relation, search=search.strip(), cursor=cursor
        )
        rows, next_cursor = page.get("items"), page.get("nextCursor")
        if not isinstance(rows, list) or len(rows) > limit:
            raise NativeAPIError(502)
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 8192
        ):
            raise NativeAPIError(502)
        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise NativeAPIError(502)
            if row.get("deleted") is True:
                continue
            username, actual = row.get("username"), row.get("relation")
            if (
                row.get("accountId", self.account) != self.account
                or not _opaque(username)
                or row.get("id") != username
                or actual not in CONTACT_RELATIONS
                or (relation != "all" and actual != relation)
            ):
                raise NativeAPIError(502)
            item = {"username": username, "relation": actual}
            for key in ("displayName", "alias"):
                if value := _text(row.get(key), 80):
                    item[key] = value
            items.append(item)
        coverage = page.get("coverage")
        return result(
            "list_contacts",
            "success",
            items=items,
            count=len(items),
            nextCursor=next_cursor,
            partial=isinstance(coverage, dict) and coverage.get("partial") is True,
            message="已查询本地联系人。" if items else "本页没有匹配的本地联系人。",
        )

    async def pages(self, search=""):
        cursor, seen = "", set()
        while True:
            page = await self.adapter.client.contact_requests(cursor=cursor, search=search)
            if not isinstance(page.get("items"), list):
                raise NativeAPIError(502)
            items = [
                item
                for item in page["items"]
                if isinstance(item, dict)
                and item.get("accountId", self.account) == self.account
                and _opaque(item.get("id"))
                and _incoming(item)
            ]
            yield page.get("asOfEventCursor"), items
            cursor = page.get("nextCursor")
            if cursor is None:
                return
            if not isinstance(cursor, str) or not cursor or len(cursor) > 8192 or cursor in seen:
                raise NativeAPIError(502)
            seen.add(cursor)

    async def list(self, search):
        if not isinstance(search, str) or len(search) > 128:
            raise ToolError("invalid_request")
        search = search.strip()
        items, count = [], 0
        async for _, page in self.pages(search):
            for item in page:
                if search.casefold() not in _text(item.get("displayName"), 512).casefold():
                    continue
                count += 1
                if len(items) < 12:
                    items.append(_preview(item))
        return result(
            "list_requests",
            "success",
            items=items,
            count=len(items),
            total=count,
            truncated=count > len(items),
            message="结果较多，请按申请人姓名缩小范围。"
            if count > len(items)
            else "查询已完成。"
            if count
            else "没有匹配的待处理好友申请。",
        )

    async def detail(self, request_id):
        item = await self.adapter.client.contact_request(request_id)
        # v2.7.1 scopes this resource via the URL and omits accountId on the row.
        if item.get("id") != request_id or item.get("accountId", self.account) != self.account:
            raise NativeAPIError(502)
        return item

    async def mutate(self, ctx, action, *, recipient_ref="", message="", request_id=""):
        from .adapter import track_send_task

        adapter = self.adapter
        admin = adapter.admin_client
        if admin is None or admin.account_id != ctx.account:
            return _outcome(action, "configuration_error")
        async with track_send_task(adapter), self._mutation_lock:
            context(ctx.event, friends=True)
            await admin.open()
            return await self._mutate(ctx, action, admin, recipient_ref, message, request_id)

    async def _mutate(self, ctx, action, admin, recipient_ref, message, request_id):
        store = self.adapter.state
        sending = action == "send_request"
        if sending:
            if (
                not _opaque(recipient_ref, 8192)
                or not isinstance(message, str)
                or len(message.encode()) > 4096
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in message)
            ):
                raise ToolError("invalid_request")
            expiry, target = self._references.get(recipient_ref, (0, None))
            if expiry <= time.time():
                self._references.pop(recipient_ref, None)
                return _outcome(action, "stale_recipient_reference")
            revision = _hash(ctx.message_id)
        else:
            if not _opaque(request_id):
                raise ToolError("invalid_request")
            target = _hash(request_id)
        existing = store.friend_operation(action, target)
        retryable = {
            "stale_contact_request",
            "stale_recipient_reference",
            "contact_request_not_actionable",
            "failed",
            "configuration_error",
            "rate_limited",
        }
        if existing and existing["status"] not in retryable:
            if existing["status"] == "contact" or (sending and existing["status"] == "submitted"):
                return _outcome(action, existing["status"], existing["operation_id"])
            return await self.inspect(
                admin, action, existing["key"], existing["operation_id"], request_id
            )
        if not sending:
            try:
                item = await self.detail(request_id)
            except NativeAPIError as error:
                if error.status == 404 or error.code in NOT_FOUND:
                    return _outcome(action, "contact_request_not_actionable")
                raise
            if item.get("isContact") is True:
                return _outcome(action, "contact")
            if not _incoming(item) or not _opaque(version := item.get("version")):
                return _outcome(action, "contact_request_not_actionable")
            revision = _hash(version)
        if existing and existing["revision_hash"] == revision:
            return _outcome(action, existing["status"], existing["operation_id"])
        context(ctx.event, friends=True)
        key = "astrbot-friend-" + _hash(
            ctx.account, self.adapter.meta().id, action, target, revision
        )
        previous_key = existing["key"] if existing else None
        if not store.start_friend_operation(action, target, revision, key, previous_key):
            return _outcome(action, "needs_review")
        try:
            if sending:
                op = await admin.send_contact_request(recipient_ref, message, key)
            else:
                op = await admin.accept_contact_request(request_id, version, key)
        except NativeAPIError as error:
            status = {
                "stale_contact_request": "stale_contact_request",
                "stale_recipient_reference": "stale_recipient_reference",
                "invalid_recipient_reference": "stale_recipient_reference",
                "contact_request_not_actionable": "contact_request_not_actionable",
            }.get(error.code)
            if status is None and 400 <= error.status < 500 and error.status not in {408, 409, 429}:
                status = error_result(action, error)["status"]
            if status:
                store.finish_friend_operation(key, status)
                return _outcome(action, status)
            op = None
        return await self.inspect(admin, action, key, None, request_id, op, refresh=True)

    async def inspect(self, admin, action, key, op_id, request_id, op=None, *, refresh=False):
        store = self.adapter.state
        status = "needs_review"
        try:
            if op is None:
                if op_id:
                    try:
                        op = await admin.operation(op_id)
                    except NativeAPIError as error:
                        if error.status not in {401, 403, 404}:
                            raise
                if op is None:
                    op = await admin.contact_mutation_by_key(key)
                refresh = False
            payload = op.get("result")
            new_id = op.get("id")
            if (
                op.get("kind") != "contact_mutation"
                or op.get("accountId") != self.account
                or not _opaque(new_id)
                or (op_id and new_id != op_id)
                or not isinstance(payload, dict)
                or payload.get("action") != action
                or (request_id and payload.get("requestId", request_id) != request_id)
            ):
                raise NativeAPIError(502)
            op_id = new_id
            state, remote = op.get("state"), payload.get("remoteOutcome")
            if state in {"queued", "running", "projecting", "submitting", "received"}:
                store.finish_friend_operation(key, "pending", op_id)
                if refresh:
                    return await self.inspect(admin, action, key, op_id, request_id)
                status = "pending"
            elif state == "succeeded" and remote == "accepted":
                status = (
                    "contact"
                    if request_id and payload.get("relationship") == "contact"
                    else ("submitted")
                )
                if request_id and status == "submitted":
                    try:
                        if (await self.detail(request_id)).get("isContact") is True:
                            status = "contact"
                    except NativeAPIError:
                        pass
            elif state == "failed" and remote in {"not_attempted", "rejected"}:
                status = (
                    "stale_contact_request"
                    if payload.get("code") == "stale_contact_request"
                    else "failed"
                )
        except NativeAPIError:
            pass
        store.finish_friend_operation(key, status, op_id)
        return _outcome(action, status, op_id)

    async def baseline(self):
        boundary, request_hashes = None, set()
        async for cursor, items in self.pages():
            if boundary is None:
                cursor_number(cursor)
                boundary = cursor
            request_hashes.update(_hash(item["id"]) for item in items)
        self.adapter.state.save_friend_baseline(request_hashes)
        return boundary

    async def notify(self, event):
        data = event["data"]
        request_id = data.get("requestId")
        if (
            data.get("source") != "remote_sync"
            or not _opaque(request_id)
            or event.get("accountId", self.account) != self.account
        ):
            return
        bindings = friend_bindings()
        if bindings is None:
            return
        adapter = self.adapter
        bot_id = adapter.meta().id
        async with bindings.friend_lock(bot_id):
            try:
                binding = await bindings.get_friend(adapter)
            except Exception:
                # Keep the event pending if its subscription cannot be read.
                raise NativeAPIError(502) from None
            request_hash = _hash(request_id)
            if (
                not binding
                or cursor_number(event["cursor"]) <= cursor_number(binding["baseline_cursor"])
                or adapter.state.has_friend_notification(request_hash)
            ):
                return
            try:
                item = await self.detail(request_id)
            except NativeAPIError as error:
                if error.status == 404 or error.code in NOT_FOUND:
                    return
                raise
            if _incoming(item) and adapter.state.claim_friend_notification(request_hash):
                status = await bindings.sender.send_friend(binding, _preview(item))
                adapter.state.finish_friend_notification(request_hash, status)

    async def cursor_expired(self):
        bindings = friend_bindings()
        if bindings:
            try:
                binding = await bindings.get_friend(self.adapter)
                if binding:
                    await bindings.sender.send_event_cursor_expired(binding, self.adapter.meta().id)
            except Exception:
                logger.warning("WeChat Native 游标故障通知失败")


async def run_friend(event, action, **params):
    try:
        ctx = context(event, friends=True)
        service = ctx.adapter.friend_requests
        if action == "search_contact":
            output = await service.search(ctx, params["query"])
        elif action == "list_contacts":
            output = await service.list_contacts(**params)
        elif action == "list_requests":
            output = await service.list(params["search"])
        else:
            output = await service.mutate(ctx, action, **params)
    except Exception as error:
        output = error_result(action, error)
    return encode(output)
