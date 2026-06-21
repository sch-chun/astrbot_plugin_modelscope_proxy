"""
ModelScope Auto Proxy — AstrBot 插件版 v0.2.0

保留原项目 core 转发逻辑，去掉 WebUI，配置项全走 AstrBot 插件配置管理。
"""
import asyncio
import threading

import uvicorn
from fastapi import FastAPI
import socket

from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

from .proxy.config import ProxyConfig
from .proxy.model_manager import ModelManager
from .proxy.api_proxy import create_proxy_router

from typing import Optional, Callable

from datetime import datetime, timedelta


@register(
    "modelscope_proxy",
    "sch-chun",
    "ModelScope 免费大模型自动代理插件",
    "0.2.0",
)
class ModelScopeProxyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._server_thread: Optional[threading.Thread] = None
        self._model_manager: Optional[ModelManager] = None
        self._proxy_config: Optional[ProxyConfig] = None
        self._fastapi_app: Optional[FastAPI] = None
        self.config: AstrBotConfig = config
        self._reset_task: Optional[asyncio.Task] = None
        self._stop_tasks: bool = False
        self._close_http_client: Optional[Callable] = None

    async def initialize(self):
        """插件初始化：读取配置 → 初始化模型管理器 → 启动代理服务"""
        if not self.config["modelscope_api_key"]:
            logger.error("❌ ModelScope API Key 未配置！请在管理面板中设置。")
            logger.error("   代理服务不会启动。请在 AstrBot 管理面板中配置 modelscope_api_key 后重启插件。")
            return
        
        model_list = self.config["model_list"]
        if not model_list:
            logger.error("❌ model_list 未配置！请在管理面板中设置至少一个模型 ID。")
            logger.error("   代理服务不会启动。请在 AstrBot 管理面板中配置 model_list 后重启插件。")
            return

        self._proxy_config = ProxyConfig(
            api_key=self.config.get("modelscope_api_key", ""),
            base_url="https://api-inference.modelscope.cn/v1",
            proxy_port=int(self.config["proxy_port"]),
            virtual_model_name=self.config["virtual_model_name"],
            show_model_tag=bool(self.config["show_model_tag"]),
            log_response=bool(self.config.get("log_response", False)),
            model_list=model_list
        )

        # 初始化模型管理器
        self._model_manager = ModelManager(model_list)

        # 创建 FastAPI 代理应用
        self._fastapi_app = FastAPI(
            title="ModelScope Proxy",
            version="0.2.0",
        )
        proxy_router, self._close_http_client = create_proxy_router(
            self._proxy_config, self._model_manager)
        self._fastapi_app.include_router(proxy_router)

        # 在后台线程中启动 uvicorn
        if not self._start_uvicorn():
            return

        logger.info(
            f"✅ ModelScope 代理服务已启动 (端口: {self._proxy_config.proxy_port})"
        )
        if self._proxy_config.model_list:
            logger.info(
                f"   模型列表: {self._proxy_config.model_list}")
        if self._proxy_config.log_response:
            logger.info("   📝 响应日志已开启（调试模式）")

        # 启动异步周期任务
        self._stop_tasks = False
        self._reset_task = asyncio.create_task(self._periodic_reset())

    async def _periodic_reset(self) -> None:
        """每天午夜重置用户额度耗尽状态"""
        while not self._stop_tasks:
            now = datetime.now()
            next_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            delay = (next_midnight - now).total_seconds()
            await asyncio.sleep(delay)
            if self._stop_tasks:
                break
            if self._model_manager:
                await self._model_manager.reset_daily_limits_if_new_day()
            logger.info("已重置用户额度耗尽状态")

    def _start_uvicorn(self) -> bool:
        """在后台线程中启动 uvicorn 服务，成功返回 True，失败（如端口被占用）返回 False"""
        assert self._fastapi_app is not None, "FastAPI app 未初始化"
        assert self._proxy_config is not None, "ProxyConfig 未初始化"

        port = self._proxy_config.proxy_port

        # 检查端口是否被占用
        if not self._is_port_available(port):
            logger.error(f"❌ 端口 {port} 已被占用，请修改配置中的 proxy_port 或释放该端口")
            logger.error(" ModelScope 代理服务启动失败")
            return False
        
        config = uvicorn.Config(
            app=self._fastapi_app,
            host="0.0.0.0",
            port=self._proxy_config.proxy_port,
            log_level="info",
            loop="asyncio",
        )
        self._uvicorn_server = uvicorn.Server(config=config)
        self._server_thread = threading.Thread(
            target=self._uvicorn_server.run,
            daemon=True,
            name="modelscope-proxy",
        )
        self._server_thread.start()
        return True

    def _is_port_available(self, port: int, host: str = "0.0.0.0") -> bool:
        """检测指定端口是否可用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
                return True
        except OSError:
            return False

    async def terminate(self) -> None:
        """插件卸载时优雅关闭服务"""
        logger.info("正在关闭 ModelScope 代理服务...")

        self._stop_tasks = True

        # 取消任务并等待它们完成
        for task in [self._reset_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
            self._uvicorn_server = None

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)

        if self._close_http_client:
            await self._close_http_client()

        logger.info("👋 ModelScope 代理服务已安全关闭")
