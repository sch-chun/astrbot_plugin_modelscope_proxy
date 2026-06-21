import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from proxy.config import ProxyConfig, VirtualModelConfig
from proxy.model_manager import ModelManager
from proxy.api_proxy import create_proxy_router


@pytest.mark.asyncio
class TestAPIProxy:
    """API 代理集成测试（使用 FastAPI TestClient）"""

    async def test_models_endpoint_returns_all_virtual_models(self, test_client, virtual_model_configs) -> None:
        """GET /v1/models 应返回所有虚拟模型名"""
        response = await test_client.get("/v1/models")
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"
        assert len(data["data"]) == len(virtual_model_configs)
        names = [item["id"] for item in data["data"]]
        expected_names = [v.name for v in virtual_model_configs]
        assert set(names) == set(expected_names)

    async def test_status_endpoint_returns_manager_status(self, test_client) -> None:
        """GET /v1/status 应返回模型管理状态（不含 total/active/models）"""
        response = await test_client.get("/v1/status")
        assert response.status_code == 200
        data = response.json()
        # 新结构不再有 total/active/models
        assert "user_quota_exhausted" in data
        assert "disabled_today" in data
        assert "cooldown_count" in data
        assert "disabled_list" in data
        assert "cooldown_list" in data
        assert "quota_reserve" in data
        assert "virtual_models" in data  # 由 api_proxy 添加
        assert isinstance(data["virtual_models"], list)

    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_success_non_stream(self, mock_get_client, test_client) -> None:
        """POST /v1/chat/completions 非流式成功响应"""
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
            "model": "test-model-1",  # 使用第一个虚拟模型
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False
        }
        response = await test_client.post("/v1/chat/completions", json=request_body)
        assert response.status_code == 200
        data = response.json()
        assert "choices" in data
        assert data["choices"][0]["message"]["content"] == "Hello from ModelScope"

    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_model_not_found_returns_404(self, mock_get_client, test_client) -> None:
        """请求不存在的虚拟模型应返回 404"""
        request_body = {
            "model": "non-existent-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False
        }
        response = await test_client.post("/v1/chat/completions", json=request_body)
        assert response.status_code == 404
        data = response.json()
        assert "not_found" in data["error"]["type"]

    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_user_quota_exhausted_returns_503(self, mock_get_client, test_client, test_model_manager) -> None:
        """用户总额度用尽时应返回 503（即使配置了 fallback 也不会调用）"""
        await test_model_manager.mark_all_disabled("测试用户配额用尽")
        request_body = {
            "model": "test-model-1",
            "messages": [{"role": "user", "content": "Hello"}]
        }
        response = await test_client.post("/v1/chat/completions", json=request_body)
        assert response.status_code == 503
        data = response.json()
        assert "quota_exhausted" in data["error"]["code"]

    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_handles_quota_headers(self, mock_get_client, test_client, test_model_manager) -> None:
        """响应头中的限额信息应被正确处理（模型额度用尽时标记禁用）"""
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
            "model": "test-model-1",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False
        }
        response = await test_client.post("/v1/chat/completions", json=request_body)
        assert response.status_code == 200
        # 模型应被标记为禁用（通过 model_manager）
        status = await test_model_manager.get_status()
        disabled_ids = [item["id"] for item in status["disabled_list"]]
        # 模型列表中的第一个模型（Qwen/Qwen3-Coder-480B）应被禁用
        assert "Qwen/Qwen3-Coder-480B" in disabled_ids

    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_global_quota_reserve_triggers_exhaustion(self, mock_get_client, test_client, test_proxy_config) -> None:
        """全局保留值触发提前禁用（剩余额度 ≤ 保留值）"""
        # 使用包含 reserve 的配置（在 conftest 中 reserve=0，这里创建新的）
        config_with_reserve = ProxyConfig(
            api_key="test_key",
            base_url="https://api-inference.modelscope.cn/v1",
            proxy_port=3473,
            proxy_host="127.0.0.1",
            show_model_tag=False,
            log_response=False,
            global_quota_reserve=5,  # 保留 5 次
            virtual_models=[VirtualModelConfig(name="test-model-1", model_list=["Qwen/Qwen3-Coder-480B"])]
        )
        mm = ModelManager(reserve=5)
        app = FastAPI()
        router, close_client = create_proxy_router(
            config_with_reserve,
            mm,
            [v.__dict__ for v in config_with_reserve.virtual_models]
        )
        app.include_router(router)

        # 模拟上游返回用户剩余 3（≤5）
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "modelscope-ratelimit-requests-remaining": "3",
            "modelscope-ratelimit-requests-limit": "100"
        }
        mock_response.json = MagicMock(return_value={"choices": [{"message": {"content": "Hello"}}]})
        mock_response.content = json.dumps(mock_response.json.return_value).encode()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            request_body = {
                "model": "test-model-1",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": False
            }
            response = await client.post("/v1/chat/completions", json=request_body)
            # 本次请求成功（因为 check_quota_headers 在收到响应后标记禁用）
            assert response.status_code == 200
            # 之后用户额度应被标记为耗尽
            assert await mm.is_user_quota_exhausted() is True

        await close_client()

    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_fallback_when_all_models_disabled(self, mock_get_client, test_client, test_model_manager, test_proxy_config) -> None:
        """当所有 ModelScope 模型不可用时，应调用兜底模型"""
        # 禁用所有 ModelScope 模型（针对 test-model-2 的列表）
        # test-model-2 有 fallback，model_list 只有一个模型
        await test_model_manager.mark_disabled("Qwen/Qwen3-393B", "test")

        # 模拟兜底 API 调用
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json = MagicMock(return_value={
            "choices": [{"message": {"content": "Fallback response"}}]
        })
        mock_response.content = b'{"choices": [{"message": {"content": "Fallback response"}}]}'
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        request_body = {
            "model": "test-model-2",  # 有 fallback 的虚拟模型
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False
        }
        response = await test_client.post("/v1/chat/completions", json=request_body)
        assert response.status_code == 200
        data = response.json()
        assert data["choices"][0]["message"]["content"] == "Fallback response"
        # 验证调用的是 fallback URL
        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        assert "fallback.api.com" in call_url

    @patch("proxy.api_proxy.get_http_client")
    async def test_chat_completion_api_key_auth(self, mock_get_client, test_client, test_proxy_config) -> None:
        """当配置了 proxy_api_key 时，未提供有效 token 应返回 401"""
        # 修改配置添加 proxy_api_key
        secure_config = ProxyConfig(
            api_key=test_proxy_config.api_key,
            base_url=test_proxy_config.base_url,
            proxy_port=test_proxy_config.proxy_port,
            proxy_host=test_proxy_config.proxy_host,
            proxy_api_key="secure-key",  # 设置验证密钥
            show_model_tag=test_proxy_config.show_model_tag,
            log_response=test_proxy_config.log_response,
            global_quota_reserve=test_proxy_config.global_quota_reserve,
            virtual_models=test_proxy_config.virtual_models
        )
        mm = ModelManager(reserve=0)
        app = FastAPI()
        router, close_client = create_proxy_router(
            secure_config,
            mm,
            [v.__dict__ for v in secure_config.virtual_models]
        )
        app.include_router(router)

        # 不提供 Authorization 头
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            request_body = {"model": "test-model-1", "messages": [{"role": "user", "content": "Hi"}]}
            response = await client.post("/v1/chat/completions", json=request_body)
            assert response.status_code == 401
            assert "invalid_api_key" in response.json()["error"]["code"]

        # 提供错误密钥
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer wrong-key"}
            response = await client.post("/v1/chat/completions", json=request_body, headers=headers)
            assert response.status_code == 401

        # 提供正确密钥
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json = MagicMock(return_value={"choices": [{"message": {"content": "OK"}}]})
        mock_response.content = b'{"choices": [{"message": {"content": "OK"}}]}'
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer secure-key"}
            response = await client.post("/v1/chat/completions", json=request_body, headers=headers)
            assert response.status_code == 200

        await close_client()


@pytest.mark.asyncio
class TestLogResponse:
    """针对 log_response 配置的测试（适配新架构）"""

    @pytest.mark.parametrize("stream,log_response,expect_log", [
        (False, True, True),
        (False, False, False),
        (True, True, True),
        (True, False, False),
    ])
    async def test_log_response_behavior(self, stream, log_response, expect_log) -> None:
        """验证 log_response 是否按预期输出日志（流式/非流式）"""
        config = ProxyConfig(
            api_key="test_key",
            base_url="https://api-inference.modelscope.cn/v1",
            proxy_port=3473,
            proxy_host="127.0.0.1",
            show_model_tag=False,
            log_response=log_response,
            global_quota_reserve=0,
            virtual_models=[VirtualModelConfig(name="test-model", model_list=["Qwen/Qwen3-Coder-480B"])]
        )
        model_manager = ModelManager(reserve=0)
        app = FastAPI()
        router, close_client = create_proxy_router(
            config,
            model_manager,
            [v.__dict__ for v in config.virtual_models]
        )
        app.include_router(router)

        mock_client = AsyncMock()

        if not stream:
            upstream_json = {"choices": [{"message": {"content": "Hello from ModelScope"}}]}
            upstream_content = json.dumps(upstream_json).encode()
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.json = MagicMock(return_value=upstream_json)
            mock_response.content = upstream_content
            mock_client.post.return_value = mock_response
        else:
            sse_chunks = [
                b'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"from "}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"ModelScope"}}]}\n\n',
                b'data: [DONE]\n\n',
            ]
            async def mock_aiter_bytes():
                for chunk in sse_chunks:
                    yield chunk

            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.aiter_bytes = mock_aiter_bytes
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

                    if stream:
                        await response.aread()

                calls = [str(call) for call in mock_log_info.call_args_list]
                if expect_log:
                    assert any("Hello from ModelScope" in c for c in calls), \
                        f"预期日志包含内容，但未找到，实际调用: {calls}"
                else:
                    assert not any("Hello from ModelScope" in c for c in calls), \
                        f"日志不应包含内容，但发现了: {calls}"

        await close_client()

    async def test_log_response_with_invalid_json(self) -> None:
        """测试上游返回无效 JSON 时日志能正常输出原始文本"""
        config = ProxyConfig(
            api_key="test_key",
            base_url="https://api-inference.modelscope.cn/v1",
            proxy_port=3473,
            proxy_host="127.0.0.1",
            show_model_tag=False,
            log_response=True,
            global_quota_reserve=0,
            virtual_models=[VirtualModelConfig(name="test-model", model_list=["Qwen/Qwen3-Coder-480B"])]
        )
        model_manager = ModelManager(reserve=0)
        app = FastAPI()
        router, close_client = create_proxy_router(
            config,
            model_manager,
            [v.__dict__ for v in config.virtual_models]
        )
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
                assert any("Not a JSON response" in c for c in calls), \
                    f"预期日志包含原始响应，但未找到，实际调用: {calls}"

        await close_client()
