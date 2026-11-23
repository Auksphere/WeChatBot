"""Small JSON tool boundary; sticker scope comes only from the trusted event."""

from .adapter import track_send_task
from .sticker_service import StickerService
from .tool_support import context, encode, error_result


async def run_sticker(event, action, *, labeler=None, **kwargs):
    try:
        ctx = context(event, stickers=True, sticker_recovery=action == "send")
        adapter = ctx.adapter
        if type(adapter.stickers) is not StickerService:
            adapter.stickers = StickerService(adapter)
        if labeler is not None and action == "find":
            kwargs["labeler"] = labeler
        async with track_send_task(adapter):
            return encode(await getattr(adapter.stickers, action)(event, **kwargs))
    except Exception as error:
        return encode(error_result(action, error))
