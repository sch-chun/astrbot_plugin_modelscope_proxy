"""
ModelScope Auto Proxy — AstrBot 插件版 v0.3.1

保留原项目 core 转发逻辑，去掉 WebUI，配置项全走 AstrBot 插件配置管理。
支持多虚拟模型配置、兜底模型、全局额度保留、API Key 验证和自定义监听地址。
初始化时自动从 ModelScope 获取可用模型列表，过滤无效配置。
"""
import asyncio
import threading
import httpx

import uvicorn
from fastapi import FastAPI
import socket

from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# ---------- 兼容导入 ----------
try:
    from astrbot.api.web import json_response
except ImportError:
    # 旧版 AstrBot 使用 quart
    from quart import jsonify as json_response
# -----------------------------

from .proxy.config import ProxyConfig, VirtualModelConfig
from .proxy.model_manager import ModelManager
from .proxy.api_proxy import create_proxy_router

from typing import Optional, Callable, List

from datetime import datetime, timedelta


@register(
    "modelscope_proxy",
    "sch-chun",
    "ModelScope 免费大模型自动代理插件",
    "0.3.0",
)
class ModelScopeProxyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._server_thread: Optional[threading.Thread] = None
        self._model_manager: Optional[ModelManager] = None
        self._proxy_config: Optional[ProxyConfig] = None
        self._fastapi_app: Optional[FastAPI] = None
        self._virtual_models: List[VirtualModelConfig] = []
        self.config: AstrBotConfig = config
        self._reset_task: Optional[asyncio.Task] = None
        self._stop_tasks: bool = False
        self._close_http_client: Optional[Callable] = None
        self._plugin_name = "modelscope_proxy"   # 插件名常量

    async def initialize(self):
        """插件初始化：读取配置 → 初始化模型管理器 → 启动代理服务"""

        # 1. 检查 API Key
        api_key = self.config.get("modelscope_api_key", "")
        if not api_key:
            logger.error("❌ ModelScope API Key 未配置！请在管理面板中设置。")
            logger.error("   代理服务不会启动。请在 AstrBot 管理面板中配置 modelscope_api_key 后重启插件。")
            return

        # 2. 解析 virtual_models 配置
        virtual_models_raw = self.config.get("virtual_models", [])
        if not virtual_models_raw:
            logger.error("❌ virtual_models 未配置！请在管理面板中至少配置一个虚拟模型。")
            logger.error("   代理服务不会启动。请在 AstrBot 管理面板中配置 virtual_models 后重启插件。")
            return

        self._virtual_models = []
        for v in virtual_models_raw:
            name = v.get("name", "modelscope-auto")
            model_list = v.get("model_list", [])
            if not model_list:
                logger.warning(f"虚拟模型 '{name}' 的 model_list 为空，跳过该配置")
                continue
            fallback = v.get("fallback", {})
            if fallback and not fallback.get("api_key"):
                logger.warning(f"虚拟模型 '{name}' 的兜底配置缺少 api_key，将不会使用兜底")
            self._virtual_models.append(VirtualModelConfig(
                name=name,
                model_list=model_list,
                fallback=fallback
            ))

        if not self._virtual_models:
            logger.error("❌ 解析后无有效的虚拟模型配置，代理服务不会启动")
            return

        # 过滤不可用的模型（从 ModelScope 获取有效列表）
        await self._filter_available_models(api_key)

        if not self._virtual_models:
            logger.error("❌ 过滤后无有效的虚拟模型配置（所有模型均不可用），代理服务不会启动")
            return

        # 3. 读取其他配置
        proxy_port = int(self.config.get("proxy_port", 3473))
        proxy_host = self.config.get("proxy_host", "127.0.0.1")
        proxy_api_key = self.config.get("proxy_api_key", "")
        show_model_tag = bool(self.config.get("show_model_tag", False))
        log_response = bool(self.config.get("log_response", False))
        global_quota_reserve = int(self.config.get("global_quota_reserve", 0))

        # 4. 创建 ProxyConfig
        self._proxy_config = ProxyConfig(
            api_key=api_key,
            base_url="https://api-inference.modelscope.cn/v1",
            proxy_port=proxy_port,
            proxy_host=proxy_host,
            proxy_api_key=proxy_api_key,
            show_model_tag=show_model_tag,
            log_response=log_response,
            global_quota_reserve=global_quota_reserve,
            virtual_models=self._virtual_models
        )

        # 5. 初始化模型管理器（传入保留值）
        self._model_manager = ModelManager(reserve=global_quota_reserve)

        # 6. 注册监控页面 Web API（必须在插件初始化时注册）
        self.context.register_web_api(
            f"/{self._plugin_name}/quota_status",
            self.quota_status_handler,
            ["GET"],
            "获取配额状态"
        )

        # 7. 创建 FastAPI 代理应用
        self._fastapi_app = FastAPI(
            title="ModelScope Proxy",
            version="0.2.0",
        )
        proxy_router, self._close_http_client = create_proxy_router(
            config=self._proxy_config,
            model_manager=self._model_manager,
            virtual_models=[v.__dict__ for v in self._virtual_models]
        )
        self._fastapi_app.include_router(proxy_router)

        # 8. 在后台线程中启动 uvicorn
        if not self._start_uvicorn():
            return

        logger.info(f"✅ ModelScope 代理服务已启动 (地址: {proxy_host}:{proxy_port})")
        if proxy_api_key:
            logger.info(f"   🔑 API Key 验证已启用")
        else:
            logger.info("   ⚠️  未启用 API Key 验证，请考虑设置 proxy_api_key 提高安全性")
        logger.info(f"   虚拟模型数量: {len(self._virtual_models)}")
        for v in self._virtual_models:
            logger.info(f"   - {v.name}: {len(v.model_list)} 个回退模型, fallback: {'有' if v.fallback.get('api_key') else '无'}")
        if log_response:
            logger.info("   📝 响应日志已开启（调试模式）")
        if global_quota_reserve > 0:
            logger.info(f"   🔒 全局额度保留值: {global_quota_reserve} 次")

        # 启动异步周期任务
        self._stop_tasks = False
        self._reset_task = asyncio.create_task(self._periodic_reset())

    async def quota_status_handler(self):
        """返回当前配额状态，供插件监控页面使用"""
        if not self._model_manager or not self._virtual_models:
            return json_response({"error": "服务未初始化"})

        status = await self._model_manager.get_status()
        disabled_set = {item["id"] for item in status.get("disabled_list", [])}
        cooldown_set = {item["id"] for item in status.get("cooldown_list", [])}

        virtual_info = []
        for v in self._virtual_models:
            models = []
            for mid in v.model_list:
                quota = status.get("model_quota", {}).get(mid)
                models.append({
                    "id": mid,
                    "remaining": quota,
                    "is_disabled": mid in disabled_set,
                    "is_cooldown": mid in cooldown_set,
                })
            virtual_info.append({
                "name": v.name,
                "models": models,
                "has_fallback": bool(v.fallback.get("api_key")),
            })

        return json_response({
            "user_quota": status.get("user_quota"),
            "user_limit": status.get("user_limit"),
            "user_quota_exhausted": status.get("user_quota_exhausted"),
            "virtual_models": virtual_info,
        })

    async def _filter_available_models(self, api_key: str) -> None:
        """从 ModelScope 获取可用模型列表，过滤掉无效的模型配置"""
        base_url = "https://api-inference.modelscope.cn/v1"
        models_url = f"{base_url}/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(models_url, headers=headers)
                if resp.status_code != 200:
                    logger.warning(f"获取 ModelScope 模型列表失败 (HTTP {resp.status_code})，跳过过滤")
                    return
                data = resp.json()
                available = set()
                for item in data.get("data", []):
                    model_id = item.get("id")
                    if model_id:
                        available.add(model_id)
                if not available:
                    logger.warning("ModelScope 返回的模型列表为空，跳过过滤")
                    return

                logger.info(f"从 ModelScope 获取到 {len(available)} 个可用模型")

                filtered_virtuals = []
                for vconf in self._virtual_models:
                    original_list = vconf.model_list
                    filtered_list = [m for m in original_list if m in available]
                    removed = [m for m in original_list if m not in available]
                    if removed:
                        logger.warning(
                            f"虚拟模型 '{vconf.name}' 中以下模型不在 ModelScope 可用列表中，已自动移除: {removed}"
                        )
                    if not filtered_list:
                        logger.warning(
                            f"虚拟模型 '{vconf.name}' 的 model_list 过滤后为空，该虚拟模型将被移除"
                        )
                        continue
                    vconf.model_list = filtered_list
                    filtered_virtuals.append(vconf)

                self._virtual_models = filtered_virtuals
                if filtered_virtuals:
                    logger.info(f"过滤后保留 {len(filtered_virtuals)} 个虚拟模型配置")

        except httpx.TimeoutException:
            logger.warning("请求 ModelScope 模型列表超时，跳过过滤，使用原始配置")
        except Exception as e:
            logger.warning(f"请求 ModelScope 模型列表发生异常: {e}，跳过过滤，使用原始配置")

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
        """在后台线程中启动 uvicorn 服务，成功返回 True，失败返回 False"""
        assert self._fastapi_app is not None, "FastAPI app 未初始化"
        assert self._proxy_config is not None, "ProxyConfig 未初始化"

        host = self._proxy_config.proxy_host
        port = self._proxy_config.proxy_port

        if not self._is_port_available(port, host):
            logger.error(f"❌ 端口 {host}:{port} 已被占用，请修改配置或释放该端口")
            logger.error("   ModelScope 代理服务启动失败")
            return False

        config = uvicorn.Config(
            app=self._fastapi_app,
            host=host,
            port=port,
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

    def _is_port_available(self, port: int, host: str = "127.0.0.1") -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
                return True
        except OSError:
            return False

    async def terminate(self) -> None:
        logger.info("正在关闭 ModelScope 代理服务...")

        self._stop_tasks = True

        if self._reset_task and not self._reset_task.done():
            self._reset_task.cancel()
            try:
                await self._reset_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"取消重置任务时发生异常: {e}")

        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
            self._uvicorn_server = None

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)

        if self._close_http_client:
            try:
                await self._close_http_client()
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    logger.debug("事件循环已关闭，HTTP 客户端已跳过关闭")
                else:
                    logger.warning(f"关闭 HTTP 客户端时发生异常: {e}")
            except Exception as e:
                logger.warning(f"关闭 HTTP 客户端时发生异常: {e}")

        logger.info("👋 ModelScope 代理服务已安全关闭")
