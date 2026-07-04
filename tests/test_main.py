import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context
from main import ModelScopeProxyPlugin


@pytest.mark.asyncio
class TestDailyRefresh:
    @pytest.fixture
    def mock_context(self) -> AsyncMock:
        """模拟 Context 对象"""
        ctx = AsyncMock(spec=Context)
        ctx.register_web_api = MagicMock()
        return ctx

    async def test_filter_available_models_updates_model_list(self, mock_context):
        """测试 _filter_available_models 正确过滤并更新 virtual_models"""
        plugin = ModelScopeProxyPlugin(mock_context, AstrBotConfig())
        api_key = "test_key"
        virtual_models = [
            {"name": "test", "model_list": ["model-a", "model-b", "model-c"], "fallback": ""}
        ]

        # 模拟 httpx 返回可用模型列表（只包含 model-a 和 model-c）
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "data": [{"id": "model-a"}, {"id": "model-c"}]
        })

        with patch("httpx.AsyncClient.get", return_value=mock_response):
            await plugin._filter_available_models(virtual_models, api_key)

        # 验证 model_list 已被更新
        assert virtual_models[0]["model_list"] == ["model-a", "model-c"]

    async def test_filter_available_models_handles_http_error(self, mock_context):
        """测试当 ModelScope 返回错误时，虚拟模型不被修改"""
        plugin = ModelScopeProxyPlugin(mock_context, AstrBotConfig())
        api_key = "test_key"
        original_models = ["model-a", "model-b"]
        virtual_models = [{"name": "test", "model_list": original_models.copy(), "fallback": ""}]

        mock_response = AsyncMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient.get", return_value=mock_response):
            await plugin._filter_available_models(virtual_models, api_key)

        # 模型列表应保持不变
        assert virtual_models[0]["model_list"] == original_models

    @pytest.mark.asyncio
    async def test_refresh_models_and_reset(self, mock_context):
        plugin = ModelScopeProxyPlugin(mock_context, AstrBotConfig())
        plugin._proxy_config = MagicMock()
        plugin._proxy_config.api_key = "test_key"
        plugin._model_manager = AsyncMock()
        plugin._virtual_models = [{"name": "test", "model_list": ["model-a"], "fallback": ""}]
        plugin._filter_available_models = AsyncMock()

        await plugin._refresh_models_and_reset()

        plugin._filter_available_models.assert_awaited_once_with(plugin._virtual_models, "test_key")
        plugin._model_manager.reset_daily_limits_if_new_day.assert_awaited_once()
