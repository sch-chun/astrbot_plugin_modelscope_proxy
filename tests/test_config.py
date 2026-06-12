import pytest
from proxy.config import ProxyConfig


class TestProxyConfig:
    """配置数据类的单元测试"""
    
    def test_default_values(self):
        config = ProxyConfig()
        
        assert config.api_key == ""
        assert config.base_url == "https://api-inference.modelscope.cn/v1"
        assert config.proxy_port == 3473
        assert config.virtual_model_name == "modelscope-auto"
        assert config.show_model_tag is False
        assert config.model_list == []
    
    def test_custom_values(self):
        config = ProxyConfig(
            api_key="custom_key",
            base_url="https://custom.api.com/v1",
            proxy_port=8080,
            virtual_model_name="custom-model",
            show_model_tag=True,
            model_list=["model-A", "model-B"]
        )
        
        assert config.api_key == "custom_key"
        assert config.base_url == "https://custom.api.com/v1"
        assert config.proxy_port == 8080
        assert config.virtual_model_name == "custom-model"
        assert config.show_model_tag is True
        assert config.model_list == ["model-A", "model-B"]
