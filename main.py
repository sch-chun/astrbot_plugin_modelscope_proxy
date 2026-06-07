"""
ModelScope Auto Proxy — AstrBot 插件版 v0.1.0

保留原项目 core 转发逻辑，去掉 WebUI，配置项全走 AstrBot 插件配置管理。
"""
import asyncio
import threading
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from proxy.config import ProxyConfig
from proxy.model_manager import ModelManager
from proxy.api_proxy import create_proxy_router


@register(
    "modelscope_proxy",
    "sch-chun",
    "ModelScope 免费大模型自动代理插件",
    "0.1.0",
)
class ModelScopeProxyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._uvicorn_server = None
        self._server_thread = None
        self._model_manager = None
        self._proxy_config = None
        self._fastapi_app = None
        self._refresh_timer = None

    async def initialize(self):
        """插件初始化：读取配置 → 初始化模型管理器 → 启动代理服务"""
        cfg = self._load_config()
        if not cfg.get("modelscope_api_key"):
            logger.error("❌ ModelScope API Key 未配置！请在管理面板中设置。")
            logger.error("   插件将启动但无法正常工作。")
            return

        self._proxy_config = ProxyConfig(
            api_key=cfg.get("modelscope_api_key", ""),
            base_url="https://api-inference.modelscope.cn/v1",
            proxy_port=int(cfg.get("proxy_port", 8000)),
            virtual_model_name=cfg.get("virtual_model_name", "modelscope-auto"),
            min_param_b=int(cfg.get("min_param_b", 4)),
            show_model_tag=bool(cfg.get("show_model_tag", False)),
            model_refresh_interval=int(cfg.get("model_refresh_interval", 86400)),
            custom_model_list=cfg.get("custom_model_list", []),
        )

        # 初始化模型管理器
        data_dir = Path(__file__).parent / "data"
        self._model_manager = ModelManager(
            data_dir=data_dir,
            min_param_b=self._proxy_config.min_param_b,
            custom_model_list=self._proxy_config.custom_model_list,
        )

        # 先尝试从缓存加载，再后台刷新
        cached = self._model_manager.load_cache()
        if not cached or self._proxy_config.custom_model_list:
            self._model_manager.refresh_models(
                self._proxy_config.api_key,
                self._proxy_config.base_url,
            )
        else:
            logger.info("已从缓存加载模型列表，后台异步刷新...")
            asyncio.create_task(self._delayed_refresh())

        # 创建 FastAPI 代理应用
        self._fastapi_app = FastAPI(
            title="ModelScope Proxy",
            version="0.1.0",
        )
        proxy_router = create_proxy_router(
            self._proxy_config, self._model_manager)
        self._fastapi_app.include_router(proxy_router)

        # 在后台线程中启动 uvicorn
        self._start_uvicorn()

        logger.info(
            f"✅ ModelScope 代理服务已启动 (端口: {self._proxy_config.proxy_port})"
        )
        logger.info(
            f"   使用模式: {'自定义列表' if self._proxy_config.custom_model_list else '自动排序'}"
        )
        if self._proxy_config.custom_model_list:
            logger.info(
                f"   自定义模型: {self._proxy_config.custom_model_list}")

        # 设置定时刷新
        interval = self._proxy_config.model_refresh_interval
        if interval > 0:
            self._refresh_timer = threading.Timer(
                interval, self._refresh_models_sync)
            self._refresh_timer.daemon = True
            self._refresh_timer.start()

    def _load_config(self) -> dict:
        """尝试从 AstrBot 上下文加载插件配置"""
        # AstrBot v3.x 插件配置获取（兼容多种方式）
        try:
            cfg = self.context.get_plugin_config()
            if cfg and isinstance(cfg, dict):
                return cfg
        except Exception:
            pass

        try:
            cfg = getattr(self, "config", None)
            if cfg and isinstance(cfg, dict):
                return cfg
        except Exception:
            pass

        try:
            raw = self.context.get_config()
            if raw and isinstance(raw, dict):
                return raw.get("modelscope_proxy", {})
        except Exception:
            pass

        logger.warning(
            "无法从 AstrBot 上下文获取配置，尝试从环境变量读取..."
        )
        import os
        return {
            "modelscope_api_key": os.getenv(
                "MODELSCOPE_API_KEY", ""),
            "proxy_port": int(os.getenv("PROXY_PORT", "8000")),
            "virtual_model_name": os.getenv(
                "VIRTUAL_MODEL_NAME", "modelscope-auto"),
            "min_param_b": int(os.getenv("MIN_PARAM_B", "4")),
        }

    def _start_uvicorn(self):
        """在后台线程中启动 uvicorn 服务"""
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

    async def _delayed_refresh(self):
        """延迟刷新模型列表（等待插件完全就绪后）"""
        await asyncio.sleep(5)
        try:
            self._model_manager.refresh_models(
                self._proxy_config.api_key,
                self._proxy_config.base_url,
            )
        except Exception as e:
            logger.error(f"后台刷新模型列表失败: {e}")

    def _refresh_models_sync(self):
        """定时刷新回调（在 Timer 线程中运行）"""
        try:
            logger.info("定时刷新模型列表...")
            self._model_manager.refresh_models(
                self._proxy_config.api_key,
                self._proxy_config.base_url,
            )
        except Exception as e:
            logger.error(f"定时刷新模型列表失败: {e}")

        # 重新注册定时器
        interval = self._proxy_config.model_refresh_interval
        if interval > 0:
            self._refresh_timer = threading.Timer(
                interval, self._refresh_models_sync)
            self._refresh_timer.daemon = True
            self._refresh_timer.start()

    async def terminate(self):
        """插件卸载时优雅关闭服务"""
        logger.info("正在关闭 ModelScope 代理服务...")

        if self._refresh_timer:
            self._refresh_timer.cancel()
            self._refresh_timer = None

        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
            self._uvicorn_server = None

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)

        logger.info("👋 ModelScope 代理服务已安全关闭")
