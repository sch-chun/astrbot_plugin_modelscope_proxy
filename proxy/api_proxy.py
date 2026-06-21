"""
API 转发模块 — 将 OpenAI 兼容请求转发到 ModelScope API-Inference，
支持多个虚拟模型配置和可选的兜底（Fallback）模型，
增加了代理服务自身的 API Key 验证和监听地址配置。
"""
import json
import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio
from typing import Optional, List, Dict, Any, AsyncGenerator

from astrbot.api import logger


MAX_RETRIES = 10
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_CLIENT_LOCK = asyncio.Lock()


class RetryableError(Exception):
    """可重试错误，代理层捕获后会尝试下一个模型"""
    pass


async def get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        async with _CLIENT_LOCK:
            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.AsyncClient(
                    timeout=httpx.Timeout(120.0, connect=30.0),
                    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
                )
    return _HTTP_CLIENT


async def close_http_client() -> None:
    """关闭全局 HTTP 客户端实例，安全处理事件循环已关闭的情况"""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        try:
            # 检查事件循环是否仍在运行
            loop = asyncio.get_running_loop()
            if loop.is_closed():
                logger.warning("事件循环已关闭，跳过 HTTP 客户端关闭")
                _HTTP_CLIENT = None
                return
            await _HTTP_CLIENT.aclose()
            _HTTP_CLIENT = None
        except RuntimeError as e:
            if "Event loop is closed" in str(e):
                logger.warning("事件循环已关闭，跳过 HTTP 客户端关闭")
                _HTTP_CLIENT = None
            else:
                logger.warning(f"关闭 HTTP 客户端时发生异常: {e}")
                _HTTP_CLIENT = None
        except Exception as e:
            logger.warning(f"关闭 HTTP 客户端时发生异常: {e}")
            _HTTP_CLIENT = None


def create_proxy_router(config, model_manager, virtual_models: List[Dict[str, Any]]) -> tuple:
    """创建代理路由，注入配置、模型管理器和虚拟模型配置列表"""
    router = APIRouter()

    # ---- 辅助函数 ----
    def _short_model_name(model_id: str) -> str:
        return model_id.split("/")[-1]

    def _inject_model_tag(content: str, model_id: str) -> str:
        return f"[{_short_model_name(model_id)}] " + content

    def _quota_exhausted_response() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "当日 API 额度已用尽",
                    "type": "quota_exhausted",
                    "code": "user_quota_exhausted",
                }
            },
        )

    def _unauthorized_response() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Unauthorized: invalid or missing API Key",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def _verify_api_key(request: Request) -> Optional[JSONResponse]:
        """验证代理服务自身 API Key，若配置了且不匹配则返回 401 响应"""
        proxy_key = config.proxy_api_key
        if not proxy_key:
            return None  # 未配置，跳过验证
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return _unauthorized_response()
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return _unauthorized_response()
        token = parts[1]
        if token != proxy_key:
            logger.warning(f"API Key 验证失败: 提供的 token 与配置不匹配")
            return _unauthorized_response()
        return None

    def _inject_tag_to_response(resp_data: dict, model_id: str) -> dict:
        try:
            choices = resp_data.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                content = msg.get("content")
                if isinstance(content, str) and content:
                    msg["content"] = _inject_model_tag(content, model_id)
                    choices[0]["message"] = msg
                    resp_data["choices"] = choices
        except Exception:
            pass
        return resp_data

    def _try_inject_tag_stream_chunk(chunk: bytes, model_id: str) -> Optional[bytes]:
        try:
            text = chunk.decode("utf-8")
            for line in text.split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    return None
                data = json.loads(data_str)
                choices = data.get("choices", [])
                if not choices:
                    return None
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if isinstance(content, str) and content:
                    delta["content"] = _inject_model_tag(content, model_id)
                    choices[0]["delta"] = delta
                    data["choices"] = choices
                    new_data_str = json.dumps(data, ensure_ascii=False)
                    new_line = f"data: {new_data_str}"
                    new_text = text.replace(line, new_line, 1)
                    return new_text.encode("utf-8")
        except Exception:
            pass
        return None

    def _log_non_stream_response(model_id: str, resp_content: Optional[bytes]) -> None:
        if resp_content is None:
            logger.info(f"[响应日志] 模型={model_id} resp_content 为 None，跳过日志")
            return
        try:
            resp_text = resp_content.decode("utf-8", errors="replace")
            if not resp_text.strip():
                logger.info(f"[响应日志] 模型={model_id} 响应为空")
                return
            try:
                resp_obj = json.loads(resp_text)
                if not isinstance(resp_obj, dict):
                    logger.info(f"[响应日志] 模型={model_id}\n{resp_text[:4000]}")
                    return
                choices = resp_obj.get("choices") or []
                if not isinstance(choices, list):
                    choices = []
                for c in choices:
                    if not isinstance(c, dict):
                        continue
                    msg = c.get("message") or {}
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content", "")
                    if not isinstance(content, str):
                        content = str(content)
                    logger.info(f"[响应日志] 模型={model_id}\n---content---\n{content}\n---end---")
                if not choices:
                    logger.info(f"[响应日志] 模型={model_id} 无 choices\n{resp_text[:4000]}")
            except (json.JSONDecodeError, ValueError):
                logger.info(f"[响应日志] 模型={model_id}\n{resp_text[:4000]}")
        except Exception as e:
            logger.info(f"[响应日志] 模型={model_id} 日志输出异常: {e}")

    def _collect_stream_text(chunk: bytes, collected: list) -> None:
        try:
            text = chunk.decode("utf-8")
            for line in text.split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    continue
                try:
                    data = json.loads(data_str)
                except (json.JSONDecodeError, ValueError):
                    continue
                choices = data.get("choices") or []
                if not isinstance(choices, list) or not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str) and content:
                    collected.append(content)
        except Exception:
            pass

    async def _call_external_api(
        url: str,
        headers: dict,
        body: dict,
        model_id: str,
        is_stream: bool,
        show_tag: bool,
        log_resp: bool,
        check_quota: bool = False,
        model_manager_ref = None,
    ) -> Response:
        client = await get_http_client()

        if not is_stream:
            # ---------- 非流式 ----------
            resp = await client.post(url, headers=headers, json=body)

            if check_quota and model_manager_ref:
                await model_manager_ref.check_quota_headers(model_id, resp.headers)

            if resp.status_code >= 400 and resp.status_code not in (404, 500, 502, 503, 400, 429):
                logger.error(f"模型 {model_id} 不可重试错误: HTTP {resp.status_code}")
                if log_resp:
                    logger.info(f"[响应日志] 模型={model_id} status={resp.status_code} body={resp.text[:2000]}")
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers={"Content-Type": "application/json"},
                )

            if check_quota and model_manager_ref:
                if resp.status_code in (404, 500, 502, 503):
                    error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    logger.warning(f"模型 {model_id} 不可恢复错误: {error_msg}")
                    if log_resp:
                        logger.info(f"[响应日志] 模型={model_id} status={resp.status_code} body={resp.text[:2000]}")
                    if await model_manager_ref.is_user_quota_exhausted():
                        return _quota_exhausted_response()
                    await model_manager_ref.mark_disabled(model_id, error_msg)
                    raise RetryableError("Model disabled due to error")

                if resp.status_code == 400:
                    error_msg = f"HTTP 400: {resp.text[:200]}"
                    logger.warning(f"模型 {model_id} 返回 400: {error_msg}")
                    if log_resp:
                        logger.info(f"[响应日志] 模型={model_id} status={resp.status_code} body={resp.text[:2000]}")
                    if await model_manager_ref.is_user_quota_exhausted():
                        return _quota_exhausted_response()
                    await model_manager_ref.mark_cooldown(model_id, error_msg)
                    raise RetryableError("Model on cooldown")

                if resp.status_code == 429:
                    logger.warning(f"模型 {model_id} 返回 429 限速")
                    await model_manager_ref.mark_429(model_id)
                    if await model_manager_ref.is_user_quota_exhausted():
                        return _quota_exhausted_response()
                    raise RetryableError("Model rate limited")

            if check_quota and model_manager_ref:
                if await model_manager_ref.is_user_quota_exhausted():
                    logger.warning(
                        f"模型 {model_id} 请求成功但用户额度已用尽，"
                        f"本次仍正常返回结果，后续请求将停止"
                    )
                await model_manager_ref.reset_429(model_id)
                logger.info(f"模型 {model_id} 请求成功")

            if show_tag:
                try:
                    resp_data = resp.json()
                    resp_data = _inject_tag_to_response(resp_data, model_id)
                    resp_content = json.dumps(resp_data, ensure_ascii=False).encode("utf-8")
                except Exception:
                    resp_content = resp.content
            else:
                resp_content = resp.content

            if log_resp:
                _log_non_stream_response(model_id, resp_content)

            return Response(
                content=resp_content,
                status_code=resp.status_code,
                headers={"Content-Type": "application/json"},
            )

        else:
            # ---------- 流式 ----------
            req = None
            try:
                req = client.stream("POST", url, headers=headers, json=body)
                resp = await req.__aenter__()

                if check_quota and model_manager_ref:
                    await model_manager_ref.check_quota_headers(model_id, resp.headers)

                if check_quota and model_manager_ref:
                    if resp.status_code in (404, 500, 502, 503):
                        error_body = await resp.aread()
                        await req.__aexit__(None, None, None)
                        error_msg = f"HTTP {resp.status_code}: {error_body.decode('utf-8', errors='replace')[:200]}"
                        logger.warning(f"模型 {model_id} 流式错误: {error_msg}")
                        if log_resp:
                            logger.info(f"[响应日志] 模型={model_id} status={resp.status_code} body={error_body.decode('utf-8', errors='replace')[:2000]}")
                        if await model_manager_ref.is_user_quota_exhausted():
                            return _quota_exhausted_response()
                        await model_manager_ref.mark_disabled(model_id, error_msg)
                        raise RetryableError("Model disabled due to error")

                    if resp.status_code == 400:
                        error_body = await resp.aread()
                        await req.__aexit__(None, None, None)
                        error_msg = f"HTTP 400: {error_body.decode('utf-8', errors='replace')[:200]}"
                        logger.warning(f"模型 {model_id} 流式 400: {error_msg}")
                        if log_resp:
                            logger.info(f"[响应日志] 模型={model_id} status={resp.status_code} body={error_body.decode('utf-8', errors='replace')[:2000]}")
                        if await model_manager_ref.is_user_quota_exhausted():
                            return _quota_exhausted_response()
                        await model_manager_ref.mark_cooldown(model_id, error_msg)
                        raise RetryableError("Model on cooldown")

                    if resp.status_code == 429:
                        await resp.aread()
                        await req.__aexit__(None, None, None)
                        logger.warning(f"模型 {model_id} 流式 429 限速")
                        await model_manager_ref.mark_429(model_id)
                        if await model_manager_ref.is_user_quota_exhausted():
                            return _quota_exhausted_response()
                        raise RetryableError("Model rate limited")

                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    await req.__aexit__(None, None, None)
                    logger.error(f"模型 {model_id} 流式不可重试: HTTP {resp.status_code}")
                    if log_resp:
                        logger.info(f"[响应日志] 模型={model_id} status={resp.status_code} body={error_body.decode('utf-8', errors='replace')[:2000]}")
                    return Response(content=error_body, status_code=resp.status_code)

                if check_quota and model_manager_ref:
                    if await model_manager_ref.is_user_quota_exhausted():
                        logger.warning(
                            f"模型 {model_id} 流式请求成功但用户额度已用尽，"
                            f"本次仍正常返回流，后续请求将停止"
                        )
                    await model_manager_ref.reset_429(model_id)
                    logger.info(f"模型 {model_id} 流式请求成功")

                collected_text = []
                injected = False

                async def stream_generator() -> AsyncGenerator:
                    nonlocal injected
                    try:
                        async for chunk in resp.aiter_bytes():
                            if log_resp:
                                _collect_stream_text(chunk, collected_text)
                            if show_tag and not injected:
                                injected_chunk = _try_inject_tag_stream_chunk(chunk, model_id)
                                if injected_chunk is not None:
                                    injected = True
                                    yield injected_chunk
                                    continue
                                else:
                                    yield chunk
                            else:
                                yield chunk
                    finally:
                        if log_resp and collected_text:
                            full_text = "".join(collected_text)
                            logger.info(f"[响应日志] 模型={model_id} (stream)\n---content---\n{full_text}\n---end---")
                        elif log_resp:
                            logger.info(f"[响应日志] 模型={model_id} (stream) 未提取到文本内容")
                        await req.__aexit__(None, None, None)

                return StreamingResponse(
                    stream_generator(),
                    status_code=resp.status_code,
                    headers={
                        "Content-Type": resp.headers.get("content-type", "text/event-stream"),
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    },
                )
            except Exception as e:
                if req is not None:
                    await req.__aexit__(None, None, None)
                raise

    # ------------------------------------------------------------
    # 路由端点
    # ------------------------------------------------------------

    @router.post("/v1/chat/completions")
    async def proxy_chat_completions(request: Request) -> Response:
        # 验证 API Key（如果配置了）
        auth_error = await _verify_api_key(request)
        if auth_error:
            return auth_error

        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": f"无效的请求体: {e}",
                        "type": "invalid_request_error",
                    }
                },
            )

        requested_model = body.get("model")
        if not requested_model:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "缺少 model 字段",
                        "type": "invalid_request_error",
                    }
                },
            )

        vconf = next((v for v in virtual_models if v.get("name") == requested_model), None)
        if not vconf:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": f"模型 '{requested_model}' 不存在",
                        "type": "not_found",
                    }
                },
            )

        model_list = vconf.get("model_list", [])
        fallback = vconf.get("fallback", {})
        is_stream = body.get("stream", False)
        show_tag = config.show_model_tag
        log_resp = config.log_response

        if await model_manager.is_user_quota_exhausted():
            if fallback:
                logger.info(f"用户额度耗尽，使用虚拟模型 '{requested_model}' 的兜底模型")
                return await _call_fallback(fallback, body, is_stream, show_tag, log_resp)
            else:
                return _quota_exhausted_response()

        for attempt in range(MAX_RETRIES):
            model = await model_manager.get_first_available(model_list)
            if model is None:
                break

            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }
            upstream_url = f"{config.base_url}/chat/completions"
            body_for_request = body.copy()
            body_for_request["model"] = model

            logger.info(f"[虚拟模型 {requested_model}] 尝试 ModelScope 模型: {model} (第 {attempt+1} 次)")

            try:
                response = await _call_external_api(
                    url=upstream_url,
                    headers=headers,
                    body=body_for_request,
                    model_id=model,
                    is_stream=is_stream,
                    show_tag=show_tag,
                    log_resp=log_resp,
                    check_quota=True,
                    model_manager_ref=model_manager,
                )
                return response
            except RetryableError:
                continue
            except Exception as e:
                logger.error(f"模型 {model} 请求发生未知异常: {e}", exc_info=True)
                await model_manager.mark_disabled(model, f"未知异常: {e}")
                continue

        if fallback:
            logger.info(f"所有 ModelScope 模型失败，使用虚拟模型 '{requested_model}' 的兜底模型")
            return await _call_fallback(fallback, body, is_stream, show_tag, log_resp)
        else:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": f"所有 ModelScope 模型均不可用，且未配置兜底模型",
                        "type": "service_unavailable",
                        "code": "all_models_unavailable",
                    }
                },
            )

    async def _call_fallback(fallback_conf: dict, body: dict, is_stream: bool, show_tag: bool, log_resp: bool) -> Response:
        api_key = fallback_conf.get("api_key")
        base_url = fallback_conf.get("base_url", "https://api.openai.com/v1")
        model_name = fallback_conf.get("model_name", "gpt-3.5-turbo")

        if not api_key:
            logger.error("兜底模型缺少 api_key，无法使用")
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "兜底模型配置不完整", "type": "configuration_error"}},
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body_for_fallback = body.copy()
        body_for_fallback["model"] = model_name
        url = f"{base_url}/chat/completions"

        try:
            response = await _call_external_api(
                url=url,
                headers=headers,
                body=body_for_fallback,
                model_id=model_name,
                is_stream=is_stream,
                show_tag=show_tag,
                log_resp=log_resp,
                check_quota=False,
                model_manager_ref=None,
            )
            return response
        except Exception as e:
            logger.error(f"兜底模型调用失败: {e}", exc_info=True)
            return JSONResponse(
                status_code=503,
                content={"error": {"message": f"兜底模型调用失败: {str(e)}", "type": "fallback_error"}},
            )

    @router.get("/v1/models")
    async def proxy_models(request: Request) -> JSONResponse:
        auth_error = await _verify_api_key(request)
        if auth_error:
            return auth_error

        data = []
        for v in virtual_models:
            data.append({
                "id": v.get("name", "modelscope-auto"),
                "object": "model",
                "owned_by": "modelscope-proxy",
                "created": 0,
            })
        return JSONResponse(content={"object": "list", "data": data})

    @router.get("/v1/status")
    async def proxy_status(request: Request) -> JSONResponse:
        auth_error = await _verify_api_key(request)
        if auth_error:
            return auth_error

        status = await model_manager.get_status()
        status["virtual_models"] = [
            {
                "name": v.get("name"),
                "model_list": v.get("model_list", []),
                "has_fallback": bool(v.get("fallback", {}).get("api_key")),
            }
            for v in virtual_models
        ]
        return JSONResponse(content=status)
    
    @router.get("/v1/quota_status")
    async def quota_status(request: Request) -> JSONResponse:
        # 验证 API Key（如果配置）
        auth_error = await _verify_api_key(request)
        if auth_error:
            return auth_error

        # 获取模型管理器状态
        status = await model_manager.get_status()
        # 构建每个虚拟模型的详细信息
        virtual_info = []
        for v in virtual_models:
            name = v.get("name")
            model_list = v.get("model_list", [])
            # 获取每个模型的剩余额度（从 status 的 model_quota 中取）
            models = []
            for mid in model_list:
                quota = status.get("model_quota", {}).get(mid)
                models.append({
                    "id": mid,
                    "remaining": quota,  # 可能为 None 表示未获取到
                    "is_disabled": mid in status.get("disabled_list", []),  # 需从 disabled_list 中判断
                    "is_cooldown": mid in [c["id"] for c in status.get("cooldown_list", [])],
                })
            virtual_info.append({
                "name": name,
                "models": models,
                "has_fallback": bool(v.get("fallback", {}).get("api_key")),
            })

        return JSONResponse(content={
            "user_quota": status.get("user_quota"),
            "user_limit": status.get("user_limit"),
            "user_quota_exhausted": status.get("user_quota_exhausted"),
            "virtual_models": virtual_info,
        })

    return router, close_http_client
