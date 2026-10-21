from fastapi import UploadFile
from app.core.exceptions import ApiError

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024


async def read_image_upload(file: UploadFile | None) -> bytes | None:
    if file is None or not file.filename:   # no file, or an empty file part
        return None
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise ApiError(400, "Profile image must be a JPEG, PNG or WebP file")
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ApiError(400, "Profile image must be 2 MB or smaller")
    return data