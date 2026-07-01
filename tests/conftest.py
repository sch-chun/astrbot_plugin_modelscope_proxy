import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from proxy.config import ProxyConfig, VirtualModelConfig
from proxy.model_manager import ModelManager
from proxy.api_proxy import create_proxy_router

from astrbot.api import AstrBotConfig
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

from typing import AsyncGenerator, Optional


# ======== 插件级别 fixtures ========

@pytest.fixture
def mock_astrbot_config() -> dict:
    """模拟 AstrBot 插件配置"""
    return {
        "modelscope_api_key": "test_api_key",
        "proxy_port": 3473,
        "proxy_host": "127.0.0.1",
        "proxy_api_key": "",  # 可选的代理验证
        "show_model_tag": True,
        "log_response": False,
        "global_quota_reserve": 0,
        "virtual_models": [
            {
                "name": "test-model-1",
                "model_list": ["Qwen/Qwen3-Coder-480B", "Qwen/Qwen3.5-397B"],
                "fallback": ""
            },
            {
                "name": "test-model-2",
                "model_list": ["Qwen/Qwen3-393B"],
                "fallback": "fallback-provider"
            }
        ]
    }


@pytest.fixture
def mock_context() -> AsyncMock:
    """模拟 AstrBot Context"""
    ctx = AsyncMock()
    ctx.send_message = AsyncMock()

    # 模拟 register_web_api
    ctx.register_web_api = MagicMock()
    return ctx


@pytest_asyncio.fixture
async def plugin_instance(mock_astrbot_config: AstrBotConfig, mock_context: AsyncMock) -> AsyncGenerator:
    """插件实例 fixture（阻止实际启动 uvicorn）"""
    from ..main import ModelScopeProxyPlugin
    plugin = ModelScopeProxyPlugin(mock_context, mock_astrbot_config)

    # 阻止实际启动 uvicorn
    plugin._start_uvicorn = MagicMock(return_value=True)

    # 阻止过滤模型（避免网络请求）
    plugin._filter_available_models = AsyncMock(return_value=None)
    await plugin.initialize()
    yield plugin
    await plugin.terminate()


# ======== 代理服务测试 fixtures ========

@pytest.fixture
def mock_provider_manager() -> Optional[AsyncMock]:
    """模拟 ProviderManager，返回一个 OpenAI 兼容的 Provider 实例"""
    mock_provider = create_autospec(ProviderOpenAIOfficial, instance=True)
    mock_provider.provider_config = {"api_base": "https://fallback.api.com/v1"}
    mock_provider.get_current_key.return_value = "fallback_key"
    mock_provider.get_model.return_value = "gpt-3.5-turbo"

    manager = AsyncMock()
    manager.get_provider_by_id = AsyncMock(return_value=mock_provider)
    return manager


@pytest.fixture
def virtual_model_configs() -> list:
    """虚拟模型配置列表（用于 ProxyConfig）"""
    return [
        VirtualModelConfig(
            name="test-model-1",
            model_list=["Qwen/Qwen3-Coder-480B", "Qwen/Qwen3.5-397B"],
            fallback=""
        ),
        VirtualModelConfig(
            name="test-model-2",
            model_list=["Qwen/Qwen3-393B"],
            fallback="fallback-provider"
        )
    ]


@pytest.fixture
def test_proxy_config(virtual_model_configs: list) -> ProxyConfig:
    """测试用代理配置（新）"""
    return ProxyConfig(
        api_key="test_key",
        base_url="https://api-inference.modelscope.cn/v1",
        proxy_port=3473,
        proxy_host="127.0.0.1",
        proxy_api_key="",  # 可设为 "secret" 测试验证
        show_model_tag=False,
        log_response=False,
        global_quota_reserve=0,
        virtual_models=virtual_model_configs
    )


@pytest.fixture
def test_model_manager() -> ModelManager:
    """测试用模型管理器（无内部模型列表）"""
    return ModelManager(reserve=0)


@pytest_asyncio.fixture
async def test_app(
    test_proxy_config: ProxyConfig,
    test_model_manager: ModelManager,
    mock_provider_manager: Optional[AsyncMock]
) -> AsyncGenerator:
    """FastAPI 测试应用实例（使用新的 create_proxy_router）"""
    app = FastAPI(title="Test ModelScope Proxy")

    # 将 VirtualModelConfig 列表转为字典列表
    virtual_models_dict = [v.__dict__ for v in test_proxy_config.virtual_models]
    router, close_client = create_proxy_router(
        test_proxy_config, test_model_manager, virtual_models_dict, mock_provider_manager
    )
    app.include_router(router)
    yield app
    await close_client()


@pytest_asyncio.fixture
async def test_client(test_app: FastAPI) -> AsyncGenerator:
    """HTTPX 异步测试客户端"""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test"
    ) as client:
        yield client
