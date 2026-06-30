from dataclasses import dataclass, field


@dataclass
class VirtualModelConfig:
    name: str
    model_list: list[str] = field(default_factory=list)
    fallback: str = ""
    timeout: int = field(default_factory=int)

@dataclass
class ProxyConfig:
    api_key: str = ""
    base_url: str = "https://api-inference.modelscope.cn/v1"
    proxy_port: int = 3473
    proxy_host: str = "127.0.0.1"
    proxy_api_key: str = ""
    show_model_tag: bool = False
    log_response: bool = False
    global_quota_reserve: int = 0
    virtual_models: list[VirtualModelConfig] = field(default_factory=list)
