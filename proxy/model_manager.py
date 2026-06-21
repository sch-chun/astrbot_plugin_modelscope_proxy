"""
模型管理模块 — 管理单个模型的禁用/冷却状态，不维护全局模型列表。
新增全局额度保留值，当用户剩余额度 ≤ 保留值时提前禁用所有模型。
"""
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional, List

from astrbot.api import logger

_429_THRESHOLD = 3
_429_COOLDOWN_SECS = 120
_400_COOLDOWN_SECS = 300

HEADER_MODEL_LIMIT = "modelscope-ratelimit-model-requests-limit"
HEADER_MODEL_REMAINING = "modelscope-ratelimit-model-requests-remaining"
HEADER_USER_LIMIT = "modelscope-ratelimit-requests-limit"
HEADER_USER_REMAINING = "modelscope-ratelimit-requests-remaining"


class ModelManager:
    def __init__(self, reserve: int = 0) -> None:
        self._lock = asyncio.Lock()
        self._disabled: dict[str, date] = {}
        self._cooldown: dict[str, datetime] = {}
        self._429_count: dict[str, int] = {}
        self._user_quota_exhausted: bool = False
        self._user_quota_exhausted_date: Optional[date] = None
        self._model_quota: dict[str, int] = {}
        self._user_quota: Optional[int] = None
        self._user_limit: Optional[int] = None
        self._reserve = reserve

    async def is_available(self, model_id: str) -> bool:
        async with self._lock:
            if model_id in self._disabled:
                return False
            if model_id in self._cooldown:
                if datetime.now() < self._cooldown[model_id]:
                    return False
                del self._cooldown[model_id]
                self._429_count.pop(model_id, None)
            return True

    async def get_first_available(self, model_list: List[str]) -> Optional[str]:
        if self._user_quota_exhausted:
            logger.warning("用户额度已用尽（或已达到保留阈值），无可用模型")
            return None
        async with self._lock:
            for model in model_list:
                if await self._is_available_locked(model):
                    return model
            return None

    async def _is_available_locked(self, model_id: str) -> bool:
        if model_id in self._disabled:
            return False
        if model_id in self._cooldown:
            if datetime.now() < self._cooldown[model_id]:
                return False
            del self._cooldown[model_id]
            self._429_count.pop(model_id, None)
        return True

    async def mark_disabled(self, model_id: str, reason: str = ""):
        async with self._lock:
            self._disabled[model_id] = date.today()
            self._429_count.pop(model_id, None)
            self._cooldown.pop(model_id, None)
        logger.warning(f"模型 {model_id} 已标记为今日不可用 (原因: {reason})")

    async def mark_cooldown(self, model_id: str, reason: str = ""):
        async with self._lock:
            self._cooldown[model_id] = datetime.now() + timedelta(seconds=_400_COOLDOWN_SECS)
        logger.warning(f"模型 {model_id} 给予 {_400_COOLDOWN_SECS // 60} 分钟冷却 (原因: {reason})")

    async def mark_429(self, model_id: str) -> bool:
        async with self._lock:
            count = self._429_count.get(model_id, 0) + 1
            self._429_count[model_id] = count
            if count >= _429_THRESHOLD:
                self._disabled[model_id] = date.today()
                self._429_count.pop(model_id, None)
                self._cooldown.pop(model_id, None)
                is_disabled = True
                logger.warning(f"模型 {model_id} 连续 {count} 次 429，视为额度耗尽，标记为今日不可用")
            else:
                self._cooldown[model_id] = datetime.now() + timedelta(seconds=_429_COOLDOWN_SECS)
                is_disabled = False
                logger.warning(f"模型 {model_id} 遭遇 429 (第 {count}/{_429_THRESHOLD} 次)，冷却 {_429_COOLDOWN_SECS // 60} 分钟")
            return is_disabled

    async def reset_429(self, model_id: str):
        async with self._lock:
            self._429_count.pop(model_id, None)

    async def check_quota_headers(self, model_id: str, resp_headers) -> tuple[bool, bool]:
        """检查响应头，如果用户剩余额度 ≤ 保留值，则触发全局禁用"""
        try:
            model_remaining = resp_headers.get(HEADER_MODEL_REMAINING)
            model_limit = resp_headers.get(HEADER_MODEL_LIMIT)
        except Exception:
            model_remaining = None
            model_limit = None

        try:
            user_remaining = resp_headers.get(HEADER_USER_REMAINING)
            user_limit = resp_headers.get(HEADER_USER_LIMIT)
        except Exception:
            user_remaining = None
            user_limit = None

        if model_remaining is None and user_remaining is None:
            return False, False

        model_exhausted = False
        user_exhausted = False

        # 模型维度
        if model_remaining is not None:
            try:
                model_rem = int(model_remaining)
                self._model_quota[model_id] = model_rem
                model_lim = int(model_limit) if model_limit else 0
                if model_rem <= 0:
                    await self.mark_quota_exhausted(model_id, remaining=model_rem, limit=model_lim)
                    model_exhausted = True
            except (ValueError, TypeError):
                pass

        # 用户维度：判断剩余额度是否 ≤ 保留值
        if user_remaining is not None:
            try:
                user_rem = int(user_remaining)
                self._user_quota = user_rem
                user_lim = int(user_limit) if user_limit else 0
                self._user_limit = user_lim
                if user_rem <= self._reserve:
                    await self.mark_all_disabled(
                        reason=f"用户剩余额度 {user_rem} ≤ 保留值 {self._reserve}，提前禁用"
                    )
                    user_exhausted = True
            except (ValueError, TypeError):
                pass

        # 日志
        if model_remaining is not None or user_remaining is not None:
            model_str = f"模型剩余: {model_remaining}/{model_limit}" if model_remaining else "模型额度: N/A"
            user_str = f"用户剩余: {user_remaining}/{user_limit}" if user_remaining else "用户额度: N/A"
            logger.debug(f"限额状态 [{model_id}] — {model_str}, {user_str}")

        return model_exhausted, user_exhausted

    async def mark_quota_exhausted(self, model_id: str, remaining: int = 0, limit: int = 0, reason: str = ""):
        async with self._lock:
            self._disabled[model_id] = date.today()
            self._429_count.pop(model_id, None)
            self._cooldown.pop(model_id, None)
        limit_str = f"/{limit}" if limit else ""
        logger.warning(f"模型 {model_id} 额度已用尽 (剩余: {remaining}{limit_str})，标记为今日不可用，原因: {reason}")

    async def mark_all_disabled(self, reason: str = ""):
        """用户额度耗尽或达到保留阈值，禁用所有模型"""
        async with self._lock:
            self._disabled.clear()
            self._cooldown.clear()
            self._429_count.clear()
            self._user_quota_exhausted = True
            self._user_quota_exhausted_date = date.today()
        logger.warning(f"全局额度耗尽或已达保留阈值 (原因: {reason})，所有模型将被禁用")

    async def is_user_quota_exhausted(self) -> bool:
        async with self._lock:
            return self._user_quota_exhausted

    async def get_status(self) -> dict:
        async with self._lock:
            today = date.today()
            now = datetime.now()
            disabled_list = [
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
            return {
                "user_quota_exhausted": self._user_quota_exhausted,
                "disabled_today": len(disabled_list),
                "cooldown_count": len(cooldown_list),
                "disabled_list": disabled_list,
                "cooldown_list": cooldown_list,
                "quota_reserve": self._reserve,
                "model_quota": self._model_quota,   # 各模型最新剩余
                "user_quota": self._user_quota,
                "user_limit": self._user_limit,
            }

    async def reset_daily_limits_if_new_day(self) -> bool:
        today = date.today()
        changed = False
        async with self._lock:
            if self._user_quota_exhausted and self._user_quota_exhausted_date != today:
                self._user_quota_exhausted = False
                self._user_quota_exhausted_date = None
                changed = True
                logger.info("跨日重置用户额度限制标志")

            expired_disabled = [mid for mid, d in self._disabled.items() if d < today]
            for mid in expired_disabled:
                del self._disabled[mid]
                changed = True

            now = datetime.now()
            expired_cooldown = [mid for mid, until in self._cooldown.items() if until < now]
            for mid in expired_cooldown:
                del self._cooldown[mid]
                self._429_count.pop(mid, None)
                changed = True

            if changed:
                logger.debug("清理过期的禁用/冷却记录完成")
        return changed
    