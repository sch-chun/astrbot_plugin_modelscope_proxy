import pytest

import base64
import io
from PIL import Image

from ..proxy.img import preprocess_image_messages, ImageConstraintError
from ..proxy.config import ProxyConfig


class TestImagePreprocess:
    """测试图片预处理功能"""

    def _create_test_image(self, width: int, height: int, format: str = "JPEG", quality: int = 95) -> str:
        """生成指定尺寸的测试图片，返回 data URL"""
        img = Image.new("RGB", (width, height), color=(255, 0, 0))
        buffer = io.BytesIO()
        if format.upper() == "JPEG":
            img.save(buffer, format="JPEG", quality=quality)
        elif format.upper() == "PNG":
            img.save(buffer, format="PNG")
        elif format.upper() == "GIF":
            img.save(buffer, format="GIF")
        else:
            raise ValueError(f"Unsupported format: {format}")
        b64 = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/{format.lower()};base64,{b64}"

    def test_no_compress_oversize_dimension(self):
        """未启用压缩时，尺寸超限应抛出 ImageConstraintError"""
        config = ProxyConfig(image_auto_compress=False)
        image_url = self._create_test_image(3000, 2000)
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_url}}]}]
        with pytest.raises(ImageConstraintError) as exc_info:
            preprocess_image_messages(messages, config)
        assert "图片超限" in str(exc_info.value)

    def test_no_compress_oversize_size(self):
        """未启用压缩时，大小超限应抛出 ImageConstraintError"""
        config = ProxyConfig(image_auto_compress=False)
        
        # 生成一张足够大的图片
        image_url = self._create_test_image(5000, 5000, format="JPEG", quality=100)
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_url}}]}]
        
        with pytest.raises(ImageConstraintError) as exc_info:
            preprocess_image_messages(messages, config)
        assert "图片超限" in str(exc_info.value)

    def test_compress_oversize_dimension(self):
        """启用压缩，尺寸超限应缩放并返回JPEG"""
        config = ProxyConfig(image_auto_compress=True)
        image_url = self._create_test_image(3000, 2000)
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_url}}]}]
        new_messages = preprocess_image_messages(messages, config)

        # 验证替换后的url是以 data:image/jpeg 开头
        new_url = new_messages[0]["content"][0]["image_url"]["url"]
        assert new_url.startswith("data:image/jpeg;base64,")

        # 解码并检查尺寸是否 ≤2048
        header, b64 = new_url.split(",", 1)
        img_data = base64.b64decode(b64)
        with Image.open(io.BytesIO(img_data)) as img:
            w, h = img.size
            assert w <= 2048 and h <= 2048

    def test_compress_oversize_size(self):
        """启用压缩，大小超限但尺寸不超，应压缩为JPEG并减小大小"""
        config = ProxyConfig(image_auto_compress=True)

        # 生成一张大尺寸图片使文件大小超限（如 3000x3000 JPEG quality 95）
        image_url = self._create_test_image(3000, 3000, quality=95)
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_url}}]}]
        new_messages = preprocess_image_messages(messages, config)
        new_url = new_messages[0]["content"][0]["image_url"]["url"]
        assert new_url.startswith("data:image/jpeg;base64,")

        # 解码检查大小是否 < 5MB
        header, b64 = new_url.split(",", 1)
        img_data = base64.b64decode(b64)
        assert len(img_data) < 5 * 1024 * 1024

    def test_messages_text_only(self):
        """纯文本消息不做修改"""
        config = ProxyConfig(image_auto_compress=False)
        messages = [{"role": "user", "content": "Hello"}]
        new_messages = preprocess_image_messages(messages, config)
        assert new_messages == messages

    def test_messages_mixed_content(self):
        """混合文本和图片，只处理图片"""
        config = ProxyConfig(image_auto_compress=True)

        # 创建一个尺寸正常的图片（不超限）
        normal_url = self._create_test_image(1000, 1000)
        messages = [{"role": "user", "content": [{"type": "text", "text": "描述这张图"}, {"type": "image_url", "image_url": {"url": normal_url}}]}]
        new_messages = preprocess_image_messages(messages, config)

        # 图片不应被修改（因为不超限）
        assert new_messages[0]["content"][1]["image_url"]["url"] == normal_url

    def test_messages_audio_ignored(self):
        """音频内容应忽略（不处理）"""
        config = ProxyConfig(image_auto_compress=True)
        audio_url = "data:audio/wav;base64,AAAA"
        messages = [{"role": "user", "content": [{"type": "audio_url", "audio_url": {"url": audio_url}}]}]
        new_messages = preprocess_image_messages(messages, config)
        
        # 不应修改
        assert new_messages == messages

    def test_messages_invalid_image_skipped(self):
        """无效图片URL跳过不处理"""
        config = ProxyConfig(image_auto_compress=True)
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "not-a-data-url"}}]}]
        new_messages = preprocess_image_messages(messages, config)

        # 未修改
        assert new_messages == messages
