from dataclasses import dataclass, field


@dataclass
class ProxyConfig:
    """ModelScope 代理配置
    
    从 AstrBot 插件配置读取，传递给各核心模块。
    """
    api_key: str = ""
    base_url: str = "https://api-inference.modelscope.cn/v1"
    proxy_port: int = 3473
    virtual_model_name: str = "modelscope-auto"
    show_model_tag: bool = False
    model_list: list = field(default_factory=list)
