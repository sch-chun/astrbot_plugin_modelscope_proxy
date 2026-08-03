from PIL import Image
import io
import base64

from .config import ProxyConfig


IMAGE_MAX_DIMENSION = 2048
IMAGE_MAX_SIZE =  5242880
MAX_QUALITY = 100
MIN_QUALITY = 10


class ImageConstraintError(Exception):
    """图片约束错误，表示图片不符合要求"""
    pass


def preprocess_image_messages(messages: list, config: ProxyConfig) -> list:
    """
    遍历消息，对 content 中的 image_url（data:image/...;base64,）进行压缩处理。
    若未启用压缩且图片超限，抛出 ImageConstraintError。
    若压缩后仍超限，亦抛出 ImageConstraintError。
    否则替换为压缩后的 data URL。
    """
    if not messages:
        return messages

    compress = config.image_auto_compress

    for msg in messages:
        content = msg.get("content")
        if not content:
            continue
        
        # 如果 content 是字符串，则跳过（纯文本）
        if isinstance(content, str):
            continue

        # 如果是数组（多模态格式）
        if isinstance(content, list):
            for item in content:
                if item.get("type") == "image_url":
                    image_url = item.get("image_url", {})
                    if not isinstance(image_url, dict):
                        continue
                    url = image_url.get("url", "")
                    if not url.startswith("data:image"):
                        continue

                    # 解析 base64
                    try:
                        header, b64 = url.split(",", 1)
                        img_data = base64.b64decode(b64)
                    except Exception:
                        continue  # 无法解析，跳过

                    # 检查当前图片
                    try:
                        with Image.open(io.BytesIO(img_data)) as img:
                            width, height = img.size
                            file_size = len(img_data)

                            # 判断是否超限
                            is_oversize = (width > IMAGE_MAX_DIMENSION or height > IMAGE_MAX_DIMENSION or file_size > IMAGE_MAX_SIZE)
                            if not is_oversize:
                                continue  # 无需处理

                            if not compress:

                                # 未启用压缩，直接抛错
                                raise ImageConstraintError(
                                    f"图片超限：尺寸 {width}x{height}，大小 {file_size/1024:.1f}KB，"
                                    f"限制 {IMAGE_MAX_DIMENSION}x{IMAGE_MAX_DIMENSION}，{IMAGE_MAX_SIZE/1024/1024:.1f}MB"
                                )

                            # ---- 压缩流程 ----
                            # 1. 统一转为 RGB（JPEG）
                            if img.mode in ("RGBA", "P"):
                                img = img.convert("RGB")

                            # 2. 等比缩放到最大边长
                            if width > height:
                                if width > IMAGE_MAX_DIMENSION:
                                    ratio = IMAGE_MAX_DIMENSION / width
                                    new_w = IMAGE_MAX_DIMENSION
                                    new_h = int(height * ratio)
                                else:
                                    new_w, new_h = width, height
                            else:
                                if height > IMAGE_MAX_DIMENSION:
                                    ratio = IMAGE_MAX_DIMENSION / height
                                    new_h = IMAGE_MAX_DIMENSION
                                    new_w = int(width * ratio)
                                else:
                                    new_w, new_h = width, height
                            if new_w != width or new_h != height:
                                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                            # 3. 先以 MAX_QUALITY 压缩为 JPEG 并检查大小
                            buffer = io.BytesIO()
                            img.save(buffer, format="JPEG", quality=MAX_QUALITY, optimize=True)
                            size = buffer.tell()
                            if size <= IMAGE_MAX_SIZE:
                                final_data = buffer.getvalue()
                            else:

                                # 二分法查找最佳质量
                                lo, hi = MIN_QUALITY, MAX_QUALITY
                                best_data = None
                                s = 0
                                while lo <= hi:
                                    mid = (lo + hi) // 2
                                    buf = io.BytesIO()
                                    img.save(buf, format="JPEG", quality=mid, optimize=True)
                                    s = buf.tell()
                                    if s <= IMAGE_MAX_SIZE:
                                        best_data = buf.getvalue()
                                        lo = mid + 1
                                    else:
                                        hi = mid - 1
                                if best_data is None:

                                    # 即使最低质量也超限，抛出异常
                                    raise ImageConstraintError(
                                        f"压缩后图片仍超限（尺寸 {new_w}x{new_h}，质量 {MIN_QUALITY} 时大小 {s/1024:.1f}KB）"
                                    )
                                final_data = best_data
                                
                            # 替换原 url
                            new_b64 = base64.b64encode(final_data).decode()
                            new_url = f"data:image/jpeg;base64,{new_b64}"
                            item["image_url"]["url"] = new_url
                    except ImageConstraintError:
                        raise
                    except Exception as e:

                        # 其他处理错误，可以选择忽略或抛错
                        raise ImageConstraintError(f"图片处理失败: {e}")
    return messages
