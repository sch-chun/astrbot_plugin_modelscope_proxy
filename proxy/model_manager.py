"""
模型管理模块 — 管理可用模型列表、故障标记、自动切换。
"""
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional

from astrbot.api import logger

_429_THRESHOLD = 3
_429_COOLDOWN_SECS = 120
_400_COOLDOWN_SECS = 300

# ModelScope 限额响应头名称
HEADER_MODEL_LIMIT = "modelscope-ratelimit-model-requests-limit"
HEADER_MODEL_REMAINING = "modelscope-ratelimit-model-requests-remaining"
HEADER_USER_LIMIT = "modelscope-ratelimit-requests-limit"
HEADER_USER_REMAINING = "modelscope-ratelimit-requests-remaining"


class ModelManager:
    """管理可用模型列表和故障标记"""

    def __init__(self, model_list: list) -> None:
        self._lock = asyncio.Lock()
        self._models: list = model_list
        self._disabled: dict[str, date] = {}
        self._cooldown: dict[str, datetime] = {}
        self._429_count: dict[str, int] = {}
        self._current_index: int = 0

        # 是否所有模型都因用户额度用尽而禁用
        self._user_quota_exhausted: bool = False
        self._user_quota_exhausted_date: Optional[date] = None

    @property
    async def models(self) -> list:
        async with self._lock:
            return list(self._models)

    def _is_available(self, model_id: str) -> bool:
        if model_id in self._disabled:
            return False
        if model_id in self._cooldown:
            if datetime.now() < self._cooldown[model_id]:
                return False
            del self._cooldown[model_id]
            self._429_count.pop(model_id, None)
            logger.info(f"模型 {model_id} 冷却已结束，重新可用")
        return True

    async def get_current_model(self) -> str | None:
        """获取当前可用的模型"""
        async with self._lock:
            if self._user_quota_exhausted:
                logger.warning("用户额度已用尽，无可用模型")
                return None

            today = date.today()
            self._disabled = {k: v for k, v in self._disabled.items()
                              if v >= today}

            for i in range(len(self._models)):
                idx = (self._current_index + i) % len(self._models)
                model = self._models[idx]
                if self._is_available(model):
                    self._current_index = idx
                    return model

            logger.error(f"所有 {len(self._models)} 个模型当前均不可用！")
            return None

    async def mark_disabled(self, model_id: str, reason: str = ""):
        """将模型标记为今日不可用"""
        async with self._lock:
            self._disabled[model_id] = date.today()
            self._429_count.pop(model_id, None)
            self._cooldown.pop(model_id, None)
            self._switch_next()
            remaining = sum(1 for m in self._models
                            if self._is_available(m))
        logger.warning(
            f"模型 {model_id} 已标记为今日不可用 (原因: {reason}), "
            f"剩余可用: {remaining}/{len(self._models)}"
        )

    async def mark_cooldown(self, model_id: str, reason: str = ""):
        """将模型标记为短期冷却"""
        async with self._lock:
            self._cooldown[model_id] = datetime.now() + \
                timedelta(seconds=_400_COOLDOWN_SECS)
            self._switch_next()
            remaining = sum(1 for m in self._models
                            if self._is_available(m))
        logger.warning(
            f"模型 {model_id} 给予 {_400_COOLDOWN_SECS // 60} 分钟冷却 "
            f"(原因: {reason}), 剩余可用: {remaining}/{len(self._models)}"
        )

    async def mark_429(self, model_id: str) -> bool:
        """记录 429 限速，返回 True 表示已触发今日禁用"""
        async with self._lock:
            count = self._429_count.get(model_id, 0) + 1
            self._429_count[model_id] = count

            if count >= _429_THRESHOLD:
                self._disabled[model_id] = date.today()
                self._429_count.pop(model_id, None)
                self._cooldown.pop(model_id, None)
                is_disabled = True
            else:
                self._cooldown[model_id] = datetime.now() + \
                    timedelta(seconds=_429_COOLDOWN_SECS)
                is_disabled = False

            self._switch_next()
            remaining = sum(1 for m in self._models
                            if self._is_available(m))

            if is_disabled:
                logger.warning(
                    f"模型 {model_id} 连续 {count} 次 429, "
                    f"视为额度耗尽，标记为今日不可用, "
                    f"剩余可用: {remaining}/{len(self._models)}"
                )
            else:
                logger.warning(
                    f"模型 {model_id} 遭遇 429 (第 {count}/{_429_THRESHOLD} 次), "
                    f"冷却 {_429_COOLDOWN_SECS // 60} 分钟, "
                    f"剩余可用: {remaining}/{len(self._models)}"
                )
            return is_disabled

    async def reset_429(self, model_id: str):
        """模型成功响应后重置 429 计数"""
        async with self._lock:
            self._429_count.pop(model_id, None)

    async def check_quota_headers(self, model_id: str,
                                   resp_headers) -> tuple[bool, bool]:
        """从 ModelScope 响应头解析限额信息，提前禁用额度用尽的模型

        比依赖 429 更精准：200 成功响应也能看出还剩多少额度。

        Args:
            model_id: 当前请求的模型 ID
            resp_headers: httpx 响应头对象（类似 dict）

        Returns:
            (model_exhausted, user_exhausted)
            - model_exhausted: 当前模型额度用尽，需要切下一个
            - user_exhausted: 用户总配额用尽，所有模型不可用
        """
        # 尝试读取模型维度额度
        try:
            model_remaining = resp_headers.get(HEADER_MODEL_REMAINING)
            model_limit = resp_headers.get(HEADER_MODEL_LIMIT)
        except Exception:
            model_remaining = None
            model_limit = None

        # 尝试读取用户维度额度
        try:
            user_remaining = resp_headers.get(HEADER_USER_REMAINING)
            user_limit = resp_headers.get(HEADER_USER_LIMIT)
        except Exception:
            user_remaining = None
            user_limit = None

        if model_remaining is None and user_remaining is None:
            # 没有限额信息，不做处理
            return False, False

        model_exhausted = False
        user_exhausted = False

        # 检查模型维度：该模型额度用尽
        if model_remaining is not None:
            try:
                model_rem = int(model_remaining)
                model_lim = int(model_limit) if model_limit else 0
                if model_rem <= 0:
                    await self.mark_quota_exhausted(
                        model_id,
                        remaining=model_rem,
                        limit=model_lim,
                        reason="模型额度用尽（通过响应头检测）",
                    )
                    model_exhausted = True
            except (ValueError, TypeError):
                pass

        # 检查用户维度：整体额度用尽
        if user_remaining is not None:
            try:
                user_rem = int(user_remaining)
                user_lim = int(user_limit) if user_limit else 0
                if user_rem <= 0:
                    await self.mark_all_disabled(
                        reason=f"用户总配额用尽 ({user_rem}/{user_lim})"
                    )
                    user_exhausted = True
            except (ValueError, TypeError):
                pass

        # 日志：记录当前额度情况
        if model_remaining is not None or user_remaining is not None:
            model_str = (f"模型剩余: {model_remaining}/{model_limit}"
                         ) if model_remaining else "模型额度: N/A"
            user_str = (f"用户剩余: {user_remaining}/{user_limit}"
                        ) if user_remaining else "用户额度: N/A"
            logger.debug(
                f"限额状态 [{model_id}] — {model_str}, {user_str}"
            )

        return model_exhausted, user_exhausted

    async def mark_quota_exhausted(self, model_id: str, remaining: int = 0,
                             limit: int = 0, reason: str = ""):
        """基于响应头中的剩余额度信息，标记模型额度用尽

        和 mark_disabled 的区别：这是在收到成功响应但剩余额度为 0 时触发，
        比等到 429 再处理更提前、更精准。
        """
        async with self._lock:
            self._disabled[model_id] = date.today()
            self._429_count.pop(model_id, None)
            self._cooldown.pop(model_id, None)
            self._switch_next()
            remaining_cnt = sum(1 for m in self._models
                                if self._is_available(m))
        limit_str = f"/{limit}" if limit else ""
        logger.warning(
            f"模型 {model_id} 额度已用尽 "
            f"(剩余: {remaining}{limit_str}), "
            f"基于响应头提前标记为今日不可用, "
            f"剩余可用: {remaining_cnt}/{len(self._models)}, "
            f"原因: {reason}"
        )

    async def mark_all_disabled(self, reason: str = ""):
        """用户整体额度用尽时，禁用所有模型"""
        async with self._lock:
            today = date.today()
            for m in self._models:
                self._disabled[m] = today
            self._429_count.clear()
            self._cooldown.clear()
            self._user_quota_exhausted = True
            self._user_quota_exhausted_date = today
        logger.warning(
            f"用户总配额已用尽 (原因: {reason}), "
            f"所有 {len(self._models)} 个模型已禁用，等待次日刷新"
        )

    async def is_user_quota_exhausted(self) -> bool:
        """检查用户额度是否已用尽"""
        async with self._lock:
            return self._user_quota_exhausted

    def _switch_next(self):
        """切换到下一个可用模型的索引"""
        for i in range(1, len(self._models)):
            next_idx = (self._current_index + i) % len(self._models)
            if self._is_available(self._models[next_idx]):
                self._current_index = next_idx
                break

    async def get_status(self) -> dict:
        """获取当前模型管理状态"""
        async with self._lock:
            today = date.today()
            now = datetime.now()

            active = [m for m in self._models
                      if self._is_available(m)]
            disabled = [
                {"id": mid, "disabled_date": d.isoformat()}
                for mid, d in self._disabled.items() if d >= today
            ]
            cooldown_list = [
                {
                    "id": mid,
                    "cooldown_until": until.isoformat(),
                    "remaining_secs": max(0, int((until - now).total_seconds())),
                }
                for mid, until in self._cooldown.items() if until > now
            ]

            current = None
            if self._models:
                current = self._models[self._current_index]
                if not self._is_available(current):
                    current = None

            return {
                "total": len(self._models),
                "active": len(active),
                "disabled_today": len(disabled),
                "cooldown_count": len(cooldown_list),
                "user_quota_exhausted": self._user_quota_exhausted,
                "current_model": current,
                "disabled_list": disabled,
                "cooldown_list": cooldown_list,
                "models": [
                    {
                        "id": m,
                        "is_active": self._is_available(m),
                        "is_cooldown": m in self._cooldown and self._cooldown[m] > now,
                        "is_disabled": m in self._disabled,
                    }
                    for m in self._models
                ],
            }

    async def reset_daily_limits_if_new_day(self) -> bool:
        """每天重置用户额度耗尽状态"""
        today = date.today()
        async with self._lock:

            # 重置用户额度标志（如果已标记且是之前的日期）
            if self._user_quota_exhausted and self._user_quota_exhausted_date != today:
                self._user_quota_exhausted = False
                self._user_quota_exhausted_date = None
                logger.info("跨日重置用户额度限制标志")

            # 同时清理过期的禁用状态
            expired_disabled = [mid for mid, d in self._disabled.items() if d < today]
            for mid in expired_disabled:
                del self._disabled[mid]

            # 清理过期的冷却状态
            now = datetime.now()
            expired_cooldown = [mid for mid, until in self._cooldown.items() if until < now]
            for mid in expired_cooldown:
                del self._cooldown[mid]
                self._429_count.pop(mid, None)

            return len(expired_disabled) > 0 or len(expired_cooldown) > 0
        