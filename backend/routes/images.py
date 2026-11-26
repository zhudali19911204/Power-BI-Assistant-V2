import os
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from ..images import MAX_UPLOAD_BYTES


def image_routes(images):
    router = APIRouter(prefix="/api")

    @router.post("/images", status_code=201)
    async def upload_image(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "单张图片不能超过 8 MB，请缩小截图范围")
            data.extend(chunk)
        return await run_in_threadpool(
            images.upload,
            bytes(data),
            unquote(request.headers.get("x-image-name", "截图")),
            request.headers.get("content-type", "").split(";", 1)[0].lower(),
        )

    @router.get("/images/{image_id}")
    def image_attachment(image_id: str):
        row = images.get(image_id)
        return Response(row["data"], media_type=row["mime_type"], headers={"Cache-Control": "no-store"})

    @router.delete("/images/{image_id}")
    def delete_image(image_id: str):
        images.delete_pending(image_id)
        return {"ok": True}

    @router.post("/desktop/screenshot")
    def start_screenshot():
        if not hasattr(os, "startfile"):
            raise HTTPException(409, "当前系统无法打开截图工具，请自行截图后粘贴图片")
        try:
            os.startfile("ms-screenclip:")
        except OSError as exc:
            raise HTTPException(409, "未能打开 Windows 截图工具，请使用 Win+Shift+S 后粘贴图片") from exc
        return {"ok": True, "detail": "截图后回到输入框按 Ctrl+V 添加图片"}

    return router
