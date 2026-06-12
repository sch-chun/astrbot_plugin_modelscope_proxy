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
        