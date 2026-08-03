from dataclasses import dataclass, field


@dataclass
class ProxyConfig:
    api_key: str = ""
    base_url: str = "https://api-inference.modelscope.cn/v1"
    proxy_port: int = 3473
    proxy_host: str = "127.0.0.1"
    proxy_api_key: str = ""
    log_response: bool = False
    global_quota_reserve: int = 0
    image_auto_compress: bool = True
    virtual_models: list[dict] = field(default_factory=list)
