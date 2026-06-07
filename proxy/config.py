from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProxyConfig:
    """ModelScope 代理配置
    
    从 AstrBot 插件配置读取，传递给各核心模块。
    """
    api_key: str = ""
    base_url: str = "https://api-inference.modelscope.cn/v1"
    proxy_port: int = 8000
    virtual_model_name: str = "modelscope-auto"
    min_param_b: int = 4
    show_model_tag: bool = False
    model_refresh_interval: int = 86400
    custom_model_list: list = field(default_factory=list)
