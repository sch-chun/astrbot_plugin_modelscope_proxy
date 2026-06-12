import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_config():
    """模拟插件配置"""
    return {
        "modelscope_api_key": "test_api_key",
        "proxy_port": 3473,
        "virtual_model_name": "modelscope-auto",
        "show_model_tag": True,
        "model_list": ["Qwen/Qwen3-Coder-480B", "Qwen/Qwen3.5-397B"]
    }


@pytest.fixture
def mock_context():
    """模拟 AstrBot Context"""
    ctx = AsyncMock()
    ctx.send_message = AsyncMock()
    return ctx


@pytest_asyncio.fixture
async def plugin_instance(mock_config, mock_context):
    """插件实例 fixture"""
    from ..main import ModelScopeProxyPlugin
    
    # 创建插件实例前确保不实际启动服务
    plugin = ModelScopeProxyPlugin(mock_context, mock_config)
    plugin._start_uvicorn = MagicMock(return_value=True)  # 阻止实际启动
    await plugin.initialize()
    yield plugin
    await plugin.terminate()


# ======== 代理测试配置 ========

from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from proxy.config import ProxyConfig
from proxy.model_manager import ModelManager
from proxy.api_proxy import create_proxy_router


@pytest.fixture
def test_proxy_config():
    """测试用代理配置"""
    return ProxyConfig(
        api_key="test_key",
        base_url="https://api-inference.modelscope.cn/v1",
        proxy_port=3473,
        virtual_model_name="test-model",
        show_model_tag=True,
        model_list=["Qwen/Qwen3-Coder-480B", "Qwen/Qwen3.5-397B"]
    )


@pytest.fixture
def test_model_manager():
    """测试用模型管理器（带模拟方法）"""
    manager = ModelManager(["Qwen/Qwen3-Coder-480B", "Qwen/Qwen3.5-397B"])
    return manager


@pytest_asyncio.fixture
async def test_app(test_proxy_config, test_model_manager):
    """FastAPI 测试应用实例"""
    app = FastAPI(title="Test ModelScope Proxy")
    router, close_client = create_proxy_router(test_proxy_config, test_model_manager)
    app.include_router(router)
    
    yield app
    
    await close_client()


@pytest_asyncio.fixture
async def test_client(test_app):
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test"
    ) as client:
        yield client
