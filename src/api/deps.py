"""
API 层通用助手 - 上传文件大小/类型限制

防止恶意超大文件上传耗尽内存（DoS），并对文件类型做白名单校验。
"""
import logging
from typing import Optional

from fastapi import HTTPException, UploadFile

logger = logging.getLogger(__name__)

# 允许的上传类型白名单（图片 + 验车视频）
ALLOWED_UPLOAD_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp",
    "video/mp4", "video/quicktime", "video/webm",
}

# 默认单文件大小上限（MB）
DEFAULT_MAX_MB = 10


async def read_upload(file: Optional[UploadFile], max_mb: int = DEFAULT_MAX_MB) -> Optional[bytes]:
    """读取上传文件内容，带类型白名单与大小限制。

    Args:
        file: 上传文件对象（可为 None）。
        max_mb: 大小上限（MB），超过则返回 413。

    Returns:
        文件字节内容；file 为 None 时返回 None。
    """
    if file is None:
        return None

    # 缺失 Content-Type 时拒绝，避免恶意客户端不带类型头绕过白名单校验
    if not file.content_type:
        raise HTTPException(status_code=415, detail="缺少 Content-Type，无法校验文件类型")

    if file.content_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(status_code=415, detail=f"不支持的文件类型: {file.content_type}")

    limit = max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"文件大小超过限制 {max_mb}MB")
    return data