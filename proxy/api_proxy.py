"""
API 转发模块 — 将 OpenAI 兼容请求转发到 ModelScope API-Inference。

使用闭包工厂模式：create_proxy_router(config, model_manager) 返回 FastAPI Router，
将所有依赖注入到路由处理函数的作用域中。
"""
import json
import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio

from typing import Optional

from astrbot.api import logger


MAX_RETRIES = 10


class RetryableError(Exception):
    """可重试错误，代理层捕获后会尝试下一个模型"""
    pass


_http_client: Optional[httpx.AsyncClient] = None  # 全局 HTTP 客户端实例


_client_lock = asyncio.Lock()  # 保护 HTTP 客户端实例的锁
async def get_http_client() -> httpx.AsyncClient:
    """获取全局 HTTP 客户端实例，确保连接池复用"""
    global _http_client
    if _http_client is None:
        async with _client_lock:
            if _http_client is None:
                _http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(120.0, connect=30.0),
                    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
                )
    return _http_client


async def close_http_client():
    """关闭全局 HTTP 客户端实例，释放资源"""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def create_proxy_router(config, model_manager):
    """创建代理路由，注入配置和模型管理器"""
    router = APIRouter()

    def _short_model_name(model_id: str) -> str:
        return model_id.split("/")[-1]

    def _inject_model_tag(content: str, model_id: str) -> str:
        return f"[{_short_model_name(model_id)}] " + content

    async def _proxy_non_stream(url, headers, body, model_id, show_tag):
        """非流式转发"""
        client = await get_http_client()
        resp = await client.post(url, headers=headers, json=body)

        # ── 限额检测（在任何状态码处理前先读取响应头）──
        model_exhausted, user_exhausted = await model_manager.check_quota_headers(
            model_id, resp.headers
        )

        # 不可重试的错误：直接返回错误响应
        if resp.status_code >= 400 and resp.status_code not in (404, 500, 502, 503, 400, 429):
            logger.error(f"模型 {model_id} 不可重试错误: HTTP {resp.status_code}")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers={"Content-Type": "application/json"},
            )
        
        # 可重试的错误：标记模型状态并抛出异常由外层捕获重试
        if resp.status_code in (404, 500, 502, 503):
            error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning(f"模型 {model_id} 不可恢复错误: {error_msg}")

            # 如果用户额度已经用尽，直接返回 503 不再尝试
            if user_exhausted:
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
            await model_manager.mark_disabled(model_id, error_msg)
            raise RetryableError("Model disabled due to error")

        if resp.status_code == 400:
            error_msg = f"HTTP 400: {resp.text[:200]}"
            logger.warning(f"模型 {model_id} 返回 400: {error_msg}")
            if user_exhausted:
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
            await model_manager.mark_cooldown(model_id, error_msg)
            raise RetryableError("Model on cooldown")

        if resp.status_code == 429:
            logger.warning(f"模型 {model_id} 返回 429 限速")

            # 如果响应头已经表明模型额度用尽，直接禁用（比计数更精准）
            if model_exhausted:
                logger.info(f"模型 {model_id} 额度已用尽（通过响应头），直接禁用")
            else:
                await model_manager.mark_429(model_id)
            if user_exhausted:
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
            raise RetryableError("Model rate limited")

        # ── 成功响应 ──
        # 即使响应成功，也要检查限额。如果用户额度已用尽，返回 503
        # （模型额度用尽则 mark_quota_exhausted 已标记，但本次请求成功仍返回结果）
        if user_exhausted:
            logger.warning(
                f"模型 {model_id} 请求成功但用户额度已用尽，"
                f"本次仍正常返回结果，后续请求将停止"
            )

        if show_tag:
            try:
                resp_data = resp.json()
                resp_data = _inject_tag_to_response(resp_data, model_id)
                resp_content = json.dumps(resp_data, ensure_ascii=False).encode("utf-8")
            except Exception:
                resp_content = resp.content
        else:
            resp_content = resp.content

        await model_manager.reset_429(model_id)

        logger.info(
            f"模型 {model_id} 请求成功"
            f"{' (额度已尽，本次为末班车)' if user_exhausted else ''}"
        )
        return Response(
            content=resp_content,
            status_code=resp.status_code,
            headers={"Content-Type": "application/json"},
        )

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

    async def _proxy_stream(url, headers, body, model_id, show_tag):
        """流式转发"""
        client = await get_http_client()
        req = None
        try:
            req = client.stream("POST", url, headers=headers, json=body)
            resp = await req.__aenter__()

            # 限额检测（在流式数据开始前检查响应头）
            model_exhausted, user_exhausted = await model_manager.check_quota_headers(
                model_id, resp.headers
            )

            if resp.status_code in (404, 500, 502, 503):
                error_body = await resp.aread()
                await req.__aexit__(None, None, None)
                error_msg = f"HTTP {resp.status_code}: {error_body.decode('utf-8', errors='replace')[:200]}"
                logger.warning(f"模型 {model_id} 流式错误: {error_msg}")
                if user_exhausted:
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
                await model_manager.mark_disabled(model_id, error_msg)
                raise RetryableError("Model disabled due to error")

            if resp.status_code == 400:
                error_body = await resp.aread()
                await req.__aexit__(None, None, None)
                error_msg = f"HTTP 400: {error_body.decode('utf-8', errors='replace')[:200]}"
                logger.warning(f"模型 {model_id} 流式 400: {error_msg}")
                if user_exhausted:
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
                await model_manager.mark_cooldown(model_id, error_msg)
                raise RetryableError("Model on cooldown")

            if resp.status_code == 429:
                await resp.aread()
                await req.__aexit__(None, None, None)
                logger.warning(f"模型 {model_id} 流式 429 限速")
                if model_exhausted:
                    logger.info(
                        f"模型 {model_id} 额度已用尽（通过响应头），直接禁用")
                else:
                    await model_manager.mark_429(model_id)
                if user_exhausted:
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
                raise RetryableError("Model rate limited")

            if resp.status_code >= 400:
                error_body = await resp.aread()
                await req.__aexit__(None, None, None)
                logger.error(f"模型 {model_id} 流式不可重试: HTTP {resp.status_code}")
                return Response(content=error_body, status_code=resp.status_code)

            # ── 流式转发成功 ──
            if user_exhausted:
                logger.warning(
                    f"模型 {model_id} 流式请求成功但用户额度已用尽，"
                    f"本次仍正常返回流，后续请求将停止"
                )

            first_chunk_done = False

            async def stream_generator():
                injected = False
                try:
                    async for chunk in resp.aiter_bytes():
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
                    await req.__aexit__(None, None, None)

            await model_manager.reset_429(model_id)
            return StreamingResponse(
                stream_generator(),
                status_code=resp.status_code,
                headers={
                    "Content-Type": resp.headers.get(
                        "content-type", "text/event-stream"),
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
        except Exception as e:
            if req is not None:
                await req.__aexit__(None, None, None)
            raise

    def _try_inject_tag_stream_chunk(chunk: bytes, model_id: str) -> bytes | None:
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

    @router.post("/v1/chat/completions")
    async def proxy_chat_completions(request: Request):
        """OpenAI 兼容的 chat completions 端点"""

        # 如果用户额度已用尽，直接返回 503，不再尝试任何模型
        if await model_manager.is_user_quota_exhausted():
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "当日 API 额度已用尽，"
                                   "请明天再试或更换 API Key",
                        "type": "quota_exhausted",
                        "code": "user_quota_exhausted",
                    }
                },
            )

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

        is_stream = body.get("stream", False)
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        upstream_url = f"{config.base_url}/chat/completions"
        show_tag = config.show_model_tag

        for retry_count in range(MAX_RETRIES):
            
            # 每次循环重新获取当前可用模型（可能会因前一次失败而切换）
            model =  await model_manager.get_current_model()
            if model is None:
                logger.error("当前无可用模型")
                break  # 退出循环，最终返回503

            original_model = body.get("model", "")
            body["model"] = model
            logger.info(f"[重试={retry_count}] 转发: model={model}, 原始model={original_model}")

            try:
                if is_stream:
                    response = await _proxy_stream(
                        upstream_url, headers, body, model, show_tag
                    )
                else:
                    response = await _proxy_non_stream(
                        upstream_url, headers, body, model, show_tag
                    )
                # 如果成功，直接返回响应
                return response
            except RetryableError:
                # 可重试错误：继续循环，尝试下一个模型
                continue
            except Exception as e:
                # 意外异常，记录并尝试下一个模型
                logger.error(f"模型 {model} 请求发生未知异常: {e}", exc_info=True)
                await model_manager.mark_disabled(model, f"未知异常: {e}")
                continue

        # 所有模型都尝试失败
        return JSONResponse(status_code=503, content={
            "error": {
                "message": f"已尝试 {MAX_RETRIES} 个模型均失败，请稍后重试",
                "type": "service_unavailable",
                "code": "max_retries_exceeded",
            }
        })

    @router.get("/v1/models")
    async def proxy_models():
        """列出对外暴露的模型（OpenAI 格式）"""
        return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": config.virtual_model_name,
                "object": "model",
                "owned_by": "modelscope-proxy",
                "created": 0,
            }
        ],
    })

    @router.get("/v1/status")
    async def proxy_status():
        """获取模型管理状态"""
        return JSONResponse(content=await model_manager.get_status())

    return router, close_http_client
