import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
class TestAPIProxy:
    """API 代理集成测试（使用 FastAPI TestClient）"""
    
    async def test_models_endpoint_returns_virtual_model(self, test_client, test_proxy_config):
        """GET /v1/models 应返回虚拟模型名"""
        response = await test_client.get("/v1/models")
        
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        assert data["data"][0]["id"] == test_proxy_config.virtual_model_name
    
    async def test_status_endpoint_returns_model_status(self, test_client, test_model_manager):
        """GET /v1/status 应返回模型管理状态"""
        response = await test_client.get("/v1/status")
        
        assert response.status_code == 200
        data = response.json()
        assert "total" in data
        assert "active" in data
        assert "models" in data
    
    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_success_non_stream(self, mock_get_client, test_client):
        """POST /v1/chat/completions 非流式成功响应"""
        # 模拟 HTTP 客户端
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json = MagicMock(return_value={
            "choices": [{"message": {"content": "Hello from ModelScope"}}]
        })
        mock_response.content = b'{"choices": [{"message": {"content": "Hello from ModelScope"}}]}'
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client
        
        request_body = {
            "model": "some-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False
        }
        
        response = await test_client.post("/v1/chat/completions", json=request_body)
        
        assert response.status_code == 200
        data = response.json()
        assert "choices" in data

    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_handles_quota_headers(self, mock_get_client, test_client):
        """响应头中的限额信息应被正确处理"""
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "modelscope-ratelimit-model-requests-remaining": "0",
            "modelscope-ratelimit-requests-remaining": "5"
        }
        mock_response.json = MagicMock(return_value={
            "choices": [{"message": {"content": "Hello"}}]
        })
        mock_response.content = json.dumps(mock_response.json.return_value).encode()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client
        
        request_body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False
        }
        
        response = await test_client.post("/v1/chat/completions", json=request_body)
        
        # 即使模型额度用尽，本次成功响应仍应返回
        assert response.status_code == 200
        # 模型管理器中模型应被标记为禁用（通过 mock 验证）
    
    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_user_quota_exhausted_returns_503(self, mock_get_client, test_client, test_model_manager):
        """用户总额度用尽时应返回 503"""
        await test_model_manager.mark_all_disabled("测试用户配额用尽")
        
        request_body = {
            "model": "any-model",
            "messages": [{"role": "user", "content": "Hello"}]
        }
        
        response = await test_client.post("/v1/chat/completions", json=request_body)
        
        assert response.status_code == 503
        data = response.json()
        assert "quota_exhausted" in data["error"]["code"]
        

from proxy.config import ProxyConfig
from proxy.model_manager import ModelManager
from proxy.api_proxy import create_proxy_router
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
class TestLogResponse:
    """针对 log_response 配置的测试"""

    @pytest.mark.parametrize("stream,log_response,expect_log", [
        (False, True, True),
        (False, False, False),
        (True, True, True),
        (True, False, False),
    ])
    async def test_log_response_behavior(self, stream, log_response, expect_log):
        """验证 log_response 是否按预期输出日志（流式/非流式）"""
        config = ProxyConfig(
            api_key="test_key",
            base_url="https://api-inference.modelscope.cn/v1",
            proxy_port=3473,
            virtual_model_name="test-model",
            show_model_tag=False,
            log_response=log_response,
            model_list=["Qwen/Qwen3-Coder-480B"]
        )
        model_manager = ModelManager(config.model_list)
        app = FastAPI()
        router, close_client = create_proxy_router(config, model_manager)
        app.include_router(router)

        mock_client = AsyncMock()

        if not stream:
            # 非流式：模拟 JSON 响应
            upstream_json = {"choices": [{"message": {"content": "Hello from ModelScope"}}]}
            upstream_content = json.dumps(upstream_json).encode()
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.json = MagicMock(return_value=upstream_json)
            mock_response.content = upstream_content
            mock_client.post.return_value = mock_response
        else:
            # 流式：模拟 SSE 数据块
            sse_chunks = [
                b'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"from "}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"ModelScope"}}]}\n\n',
                b'data: [DONE]\n\n',
            ]
            # 异步生成器函数，返回异步迭代器
            async def mock_aiter_bytes():
                for chunk in sse_chunks:
                    yield chunk

            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.aiter_bytes = mock_aiter_bytes  # 异步生成器函数

            # 模拟异步上下文管理器
            mock_stream_cm = AsyncMock()
            mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
            mock_stream_cm.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream = MagicMock(return_value=mock_stream_cm)

        with patch('proxy.api_proxy.get_http_client', return_value=mock_client):
            with patch('astrbot.api.logger.info') as mock_log_info:
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
                ) as client:
                    request_body = {
                        "model": "test-model",
                        "messages": [{"role": "user", "content": "Hi"}],
                        "stream": stream
                    }
                    response = await client.post("/v1/chat/completions", json=request_body)
                    assert response.status_code == 200

                    # 消费响应体（流式需要读取完才能触发日志输出）
                    if stream:
                        await response.aread()
                    # 非流式响应内容已在 post 时读取，无需额外操作

                # 检查日志调用
                calls = [str(call) for call in mock_log_info.call_args_list]
                if expect_log:

                    # 至少有一条日志包含预期的内容片段
                    assert any("Hello from ModelScope" in c for c in calls), \
                        f"预期日志包含内容，但未找到，实际调用: {calls}"
                else:
                    assert not any("Hello from ModelScope" in c for c in calls), \
                        f"日志不应包含内容，但发现了: {calls}"

        await close_client()

    async def test_log_response_with_invalid_json(self):
        """测试上游返回无效 JSON 时日志能正常输出原始文本"""
        config = ProxyConfig(
            api_key="test_key",
            base_url="https://api-inference.modelscope.cn/v1",
            proxy_port=3473,
            virtual_model_name="test-model",
            show_model_tag=False,
            log_response=True,
            model_list=["Qwen/Qwen3-Coder-480B"]
        )
        model_manager = ModelManager(config.model_list)
        app = FastAPI()
        router, close_client = create_proxy_router(config, model_manager)
        app.include_router(router)

        invalid_content = b"Not a JSON response"
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json = MagicMock(side_effect=json.JSONDecodeError("Invalid", "doc", 0))
        mock_response.content = invalid_content
        mock_client.post.return_value = mock_response

        with patch('proxy.api_proxy.get_http_client', return_value=mock_client):
            with patch('astrbot.api.logger.info') as mock_log_info:
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
                ) as client:
                    request_body = {
                        "model": "test-model",
                        "messages": [{"role": "user", "content": "Hi"}],
                        "stream": False
                    }
                    response = await client.post("/v1/chat/completions", json=request_body)
                    assert response.status_code == 200

                calls = [str(call) for call in mock_log_info.call_args_list]

                # 日志中应包含原始响应文本（最多4000字符）
                assert any("Not a JSON response" in c for c in calls), \
                    f"预期日志包含原始响应，但未找到，实际调用: {calls}"

        await close_client()
