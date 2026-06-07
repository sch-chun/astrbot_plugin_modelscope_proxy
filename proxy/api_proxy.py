"""
API 转发模块 — 将 OpenAI 兼容请求转发到 ModelScope API-Inference。

使用闭包工厂模式：create_proxy_router(config, model_manager) 返回 FastAPI Router，
将所有依赖注入到路由处理函数的作用域中。
"""
import json
import logging
import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from .stats import stats_collector

logger = logging.getLogger(__name__)

MAX_RETRIES = 10


def create_proxy_router(config, model_manager):
    """创建代理路由，注入配置和模型管理器"""
    router = APIRouter()
    stats = stats_collector

    def _short_model_name(model_id: str) -> str:
        return model_id.split("/")[-1]

    def _inject_model_tag(content: str, model_id: str) -> str:
        return f"[{_short_model_name(model_id)}] " + content

    def _extract_usage(resp_data: dict) -> tuple:
        usage = resp_data.get("usage", {})
        if not usage:
            return 0, 0, 0
        return (
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            int(usage.get("total_tokens", 0)),
        )

    async def _proxy_non_stream(url, headers, body, model_id,
                                request, retry_count, show_tag):
        """非流式转发"""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=body)

        # ── 限额检测（在任何状态码处理前先读取响应头）──
        model_exhausted, user_exhausted = model_manager.check_quota_headers(
            model_id, resp.headers
        )

        if resp.status_code in (404, 500, 502, 503):
            error_msg = f"HTTP {resp.status_code}"
            try:
                error_detail = resp.json()
                error_msg = f"HTTP {resp.status_code}: {json.dumps(error_detail, ensure_ascii=False)[:300]}"
            except Exception:
                error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning(f"模型 {model_id} 不可恢复错误: {error_msg}")
            stats.record_error(model_id, resp.status_code)
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
            model_manager.mark_disabled(model_id, error_msg)
            return await proxy_chat_completions(request, retry_count + 1)

        if resp.status_code == 400:
            error_msg = f"HTTP 400"
            try:
                error_detail = resp.json()
                error_msg = f"HTTP 400: {json.dumps(error_detail, ensure_ascii=False)[:300]}"
            except Exception:
                error_msg = f"HTTP 400: {resp.text[:200]}"
            logger.warning(f"模型 {model_id} 返回 400: {error_msg}")
            stats.record_error(model_id, 400)
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
            model_manager.mark_cooldown(model_id, error_msg)
            return await proxy_chat_completions(request, retry_count + 1)

        if resp.status_code == 429:
            logger.warning(f"模型 {model_id} 返回 429 限速")
            stats.record_error(model_id, 429)
            # 如果响应头已经表明模型额度用尽，直接禁用（比计数更精准）
            if model_exhausted:
                logger.info(f"模型 {model_id} 额度已用尽（通过响应头），直接禁用")
            else:
                model_manager.mark_429(model_id)
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
            return await proxy_chat_completions(request, retry_count + 1)

        if resp.status_code >= 400:
            logger.error(f"模型 {model_id} 不可重试错误: HTTP {resp.status_code}")
            stats.record_error(model_id, resp.status_code)
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers={"Content-Type": "application/json"},
            )

        # ── 成功响应 ──
        # 即使响应成功，也要检查限额。如果用户额度已用尽，返回 503
        # （模型额度用尽则 mark_quota_exhausted 已标记，但本次请求成功仍返回结果）
        if user_exhausted:
            logger.warning(
                f"模型 {model_id} 请求成功但用户额度已用尽，"
                f"本次仍正常返回结果，后续请求将停止"
            )

        try:
            resp_data = resp.json()
            prompt_t, comp_t, total_t = _extract_usage(resp_data)
            stats.record_success(model_id, prompt_t, comp_t, total_t)
            model_manager.reset_429(model_id)

            if show_tag:
                resp_data = _inject_tag_to_response(resp_data, model_id)
                resp_content = json.dumps(
                    resp_data, ensure_ascii=False).encode("utf-8")
            else:
                resp_content = resp.content
        except Exception:
            stats.record_success(model_id)
            resp_content = resp.content

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

    async def _proxy_stream(url, headers, body, model_id,
                            request, retry_count, show_tag):
        """流式转发"""
        client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))
        try:
            req = client.stream("POST", url, headers=headers, json=body)
            resp = await req.__aenter__()

            # 限额检测（在流式数据开始前检查响应头）
            model_exhausted, user_exhausted = model_manager.check_quota_headers(
                model_id, resp.headers
            )

            if resp.status_code in (404, 500, 502, 503):
                error_body = await resp.aread()
                await req.__aexit__(None, None, None)
                await client.aclose()
                error_msg = f"HTTP {resp.status_code}: {error_body.decode('utf-8', errors='replace')[:200]}"
                logger.warning(f"模型 {model_id} 流式错误: {error_msg}")
                stats.record_error(model_id, resp.status_code)
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
                model_manager.mark_disabled(model_id, error_msg)
                return await proxy_chat_completions(request, retry_count + 1)

            if resp.status_code == 400:
                error_body = await resp.aread()
                await req.__aexit__(None, None, None)
                await client.aclose()
                error_msg = f"HTTP 400: {error_body.decode('utf-8', errors='replace')[:200]}"
                logger.warning(f"模型 {model_id} 流式 400: {error_msg}")
                stats.record_error(model_id, 400)
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
                model_manager.mark_cooldown(model_id, error_msg)
                return await proxy_chat_completions(request, retry_count + 1)

            if resp.status_code == 429:
                await resp.aread()
                await req.__aexit__(None, None, None)
                await client.aclose()
                logger.warning(f"模型 {model_id} 流式 429 限速")
                stats.record_error(model_id, 429)
                if model_exhausted:
                    logger.info(
                        f"模型 {model_id} 额度已用尽（通过响应头），直接禁用")
                else:
                    model_manager.mark_429(model_id)
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
                return await proxy_chat_completions(request, retry_count + 1)

            if resp.status_code >= 400:
                error_body = await resp.aread()
                await req.__aexit__(None, None, None)
                await client.aclose()
                logger.error(f"模型 {model_id} 流式不可重试: HTTP {resp.status_code}")
                stats.record_error(model_id, resp.status_code)
                return Response(content=error_body, status_code=resp.status_code)

            # ── 流式转发成功 ──
            if user_exhausted:
                logger.warning(
                    f"模型 {model_id} 流式请求成功但用户额度已用尽，"
                    f"本次仍正常返回流，后续请求将停止"
                )

            first_chunk_done = False
            usage_buffer = []

            async def stream_generator():
                nonlocal first_chunk_done
                try:
                    async for chunk in resp.aiter_bytes():
                        if show_tag and not first_chunk_done:
                            injected = _try_inject_tag_stream_chunk(
                                chunk, model_id)
                            if injected is not None:
                                first_chunk_done = True
                                yield injected
                                continue
                        usage_buffer.append(chunk)
                        if len(usage_buffer) > 3:
                            usage_buffer.pop(0)
                        yield chunk
                finally:
                    _extract_and_record_stream_usage(usage_buffer, model_id)
                    await req.__aexit__(None, None, None)
                    await client.aclose()

            model_manager.reset_429(model_id)
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
            await client.aclose()
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

    def _extract_and_record_stream_usage(chunks, model_id):
        try:
            for chunk in reversed(chunks):
                text = chunk.decode("utf-8", errors="replace")
                for line in text.split("\n"):
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        continue
                    try:
                        data = json.loads(data_str)
                        if "usage" in data:
                            p, c, t = _extract_usage(data)
                            stats.record_success(model_id, p, c, t)
                            return
                    except Exception:
                        continue
            stats.record_success(model_id)
        except Exception:
            stats.record_success(model_id)

    @router.post("/v1/chat/completions")
    async def proxy_chat_completions(request: Request,
                                     _retry_count: int = 0):
        """OpenAI 兼容的 chat completions 端点"""
        # 如果用户额度已用尽，直接返回 503，不再尝试任何模型
        if model_manager.is_user_quota_exhausted():
            logger.warning("用户额度已用尽，拒绝请求")
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

        if _retry_count >= MAX_RETRIES:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": f"已尝试 {MAX_RETRIES} 个模型均失败，请稍后重试",
                        "type": "service_unavailable",
                        "code": "max_retries_exceeded",
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

        model = model_manager.get_current_model()
        if model is None:
            logger.error("所有模型当前均不可用")
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "所有模型当前均不可用，请稍后重试",
                        "type": "service_unavailable",
                        "code": "all_models_disabled",
                    }
                },
            )

        original_model = body.get("model", "")
        body["model"] = model["id"]
        logger.info(
            f"[重试={_retry_count}] 转发: model={model['id']} "
            f"({model['param_b']}B), 原始model={original_model}"
        )

        stats.record_request(model["id"])

        is_stream = body.get("stream", False)
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        upstream_url = f"{config.base_url}/chat/completions"
        show_tag = config.show_model_tag

        try:
            if is_stream:
                return await _proxy_stream(
                    upstream_url, headers, body, model["id"],
                    request, _retry_count, show_tag,
                )
            else:
                return await _proxy_non_stream(
                    upstream_url, headers, body, model["id"],
                    request, _retry_count, show_tag,
                )
        except httpx.TimeoutException:
            logger.warning(f"模型 {model['id']} 请求超时")
            stats.record_error(model["id"], 504)
            model_manager.mark_disabled(model["id"], "请求超时")
            return await proxy_chat_completions(request, _retry_count + 1)
        except httpx.ConnectError as e:
            logger.warning(f"模型 {model['id']} 连接失败: {e}")
            stats.record_error(model["id"], 503)
            model_manager.mark_disabled(model["id"], f"连接失败: {e}")
            return await proxy_chat_completions(request, _retry_count + 1)
        except Exception as e:
            logger.error(f"模型 {model['id']} 请求异常: {e}")
            stats.record_error(model["id"], 500)
            model_manager.mark_disabled(model["id"], f"请求异常: {e}")
            return await proxy_chat_completions(request, _retry_count + 1)

    @router.get("/v1/models")
    async def proxy_models():
        """列出可用模型（OpenAI 格式）"""
        status = model_manager.get_status()
        model_list = []
        for m in status["models"]:
            model_list.append({
                "id": m["id"],
                "object": "model",
                "owned_by": m.get("owned_by", "unknown"),
                "created": m.get("created", 0),
                "param_b": m.get("param_b", 0),
                "is_active": m.get("is_active", True),
            })
        return JSONResponse(content={
            "object": "list",
            "data": model_list,
        })

    @router.get("/v1/status")
    async def proxy_status():
        """获取模型管理状态"""
        return JSONResponse(content=model_manager.get_status())

    return router
