from ..proxy.config import ProxyConfig


class TestProxyConfig:
    def test_default_values(self) -> None:
        c = ProxyConfig()
        assert c.api_key == ""
        assert c.base_url == "https://api-inference.modelscope.cn/v1"
        assert c.proxy_port == 3473
        assert c.proxy_host == "127.0.0.1"
        assert c.proxy_api_key == ""
        assert c.log_response is False
        assert c.global_quota_reserve == 0
        assert c.virtual_models == []

    def test_custom_values(self, virtual_model_configs) -> None:
        v1, v2 = virtual_model_configs
        c = ProxyConfig(
            api_key="custom_key",
            base_url="https://custom.api/v1",
            proxy_port=8080,
            proxy_host="0.0.0.0",
            proxy_api_key="secret",
            log_response=True,
            global_quota_reserve=10,
            virtual_models=[v1, v2]
        )
        assert c.api_key == "custom_key"
        assert c.proxy_port == 8080
        assert c.proxy_host == "0.0.0.0"
        assert c.proxy_api_key == "secret"
        assert c.global_quota_reserve == 10
        assert len(c.virtual_models) == 2
        assert c.virtual_models[0]["name"] == "test-model-1"
        assert c.virtual_models[1]["fallback"] == "fallback-provider"
