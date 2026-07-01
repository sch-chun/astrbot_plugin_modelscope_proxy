import pytest
from datetime import date, timedelta
from proxy.model_manager import ModelManager, HEADER_MODEL_REMAINING, HEADER_MODEL_LIMIT, HEADER_USER_REMAINING, HEADER_USER_LIMIT
from unittest.mock import MagicMock


class TestModelManager:
    """模型管理器单元测试（无内部模型列表）"""

    def test_init(self) -> None:
        mm = ModelManager(reserve=0)
        assert mm._disabled == {}
        assert mm._cooldown == {}
        assert mm._429_count == {}
        assert mm._user_quota_exhausted is False
        assert mm._reserve == 0

    @pytest.mark.asyncio
    async def test_is_available_initial(self) -> None:
        mm = ModelManager()
        assert await mm.is_available("model-A") is True

    @pytest.mark.asyncio
    async def test_is_available_disabled(self) -> None:
        mm = ModelManager()
        await mm.mark_disabled("model-A", "test")
        assert await mm.is_available("model-A") is False

    @pytest.mark.asyncio
    async def test_is_available_cooldown(self) -> None:
        mm = ModelManager()
        await mm.mark_cooldown("model-A", "test")
        assert await mm.is_available("model-A") is False  # 冷却中

    @pytest.mark.asyncio
    async def test_get_first_available_returns_available(self) -> None:
        mm = ModelManager()
        model_list = ["model-A", "model-B"]

        # 默认全部可用
        result = await mm.get_first_available(model_list)
        assert result == "model-A"

    @pytest.mark.asyncio
    async def test_get_first_available_skips_disabled(self) -> None:
        mm = ModelManager()
        await mm.mark_disabled("model-A")
        model_list = ["model-A", "model-B"]
        result = await mm.get_first_available(model_list)
        assert result == "model-B"

    @pytest.mark.asyncio
    async def test_get_first_available_returns_none_if_user_quota_exhausted(self) -> None:
        mm = ModelManager()
        await mm.mark_all_disabled("user quota exhausted")
        model_list = ["model-A"]
        result = await mm.get_first_available(model_list)
        assert result is None

    @pytest.mark.asyncio
    async def test_mark_disabled(self) -> None:
        mm = ModelManager()
        await mm.mark_disabled("model-A", "reason")
        assert "model-A" in mm._disabled
        assert await mm.is_available("model-A") is False

    @pytest.mark.asyncio
    async def test_mark_cooldown(self) -> None:
        mm = ModelManager()
        await mm.mark_cooldown("model-A", "reason")
        assert "model-A" in mm._cooldown
        assert await mm.is_available("model-A") is False

    @pytest.mark.asyncio
    async def test_mark_429_first_time_cooldown(self) -> None:
        mm = ModelManager()
        is_disabled = await mm.mark_429("model-A")
        assert is_disabled is False
        assert "model-A" in mm._cooldown
        assert mm._429_count["model-A"] == 1

    @pytest.mark.asyncio
    async def test_mark_429_third_time_disables(self) -> None:
        mm = ModelManager()
        await mm.mark_429("model-A")  # 1
        await mm.mark_429("model-A")  # 2
        is_disabled = await mm.mark_429("model-A")  # 3
        assert is_disabled is True
        assert "model-A" in mm._disabled
        assert "model-A" not in mm._cooldown
        assert "model-A" not in mm._429_count

    @pytest.mark.asyncio
    async def test_reset_429_clears_counter(self) -> None:
        mm = ModelManager()
        await mm.mark_429("model-A")
        assert mm._429_count["model-A"] == 1
        await mm.reset_429("model-A")
        assert "model-A" not in mm._429_count

    @pytest.mark.asyncio
    async def test_check_quota_headers_detects_model_exhaustion(self) -> None:
        mm = ModelManager(reserve=0)
        mock_headers = MagicMock()
        mock_headers.get.side_effect = lambda key, default=None: {
            HEADER_MODEL_REMAINING: "0",
            HEADER_MODEL_LIMIT: "100",
        }.get(key, default)
        model_ex, user_ex = await mm.check_quota_headers("model-A", mock_headers)
        assert model_ex is True
        assert "model-A" in mm._disabled

    @pytest.mark.asyncio
    async def test_check_quota_headers_detects_user_exhaustion_with_reserve(self) -> None:
        mm = ModelManager(reserve=5)  # 保留 5 次
        mock_headers = MagicMock()
        mock_headers.get.side_effect = lambda key, default=None: {
            HEADER_USER_REMAINING: "3",   # 剩余 3 ≤ 5 → 触发
            HEADER_USER_LIMIT: "100",
        }.get(key, default)
        model_ex, user_ex = await mm.check_quota_headers("model-A", mock_headers)
        assert user_ex is True
        assert mm._user_quota_exhausted is True

    @pytest.mark.asyncio
    async def test_mark_all_disabled_sets_flag(self) -> None:
        mm = ModelManager()
        await mm.mark_all_disabled("test")
        assert mm._user_quota_exhausted is True
        assert mm._user_quota_exhausted_date == date.today()
        
        # 清空状态字典
        assert mm._disabled == {}
        assert mm._cooldown == {}
        assert mm._429_count == {}

    @pytest.mark.asyncio
    async def test_is_user_quota_exhausted(self) -> None:
        mm = ModelManager()
        assert await mm.is_user_quota_exhausted() is False
        await mm.mark_all_disabled("test")
        assert await mm.is_user_quota_exhausted() is True

    @pytest.mark.asyncio
    async def test_reset_daily_limits_clears_expired_and_user_flag(self) -> None:
        mm = ModelManager()

        # 设置过期的禁用和用户标志
        old_date = date.today() - timedelta(days=1)
        mm._disabled["model-A"] = old_date
        mm._user_quota_exhausted = True
        mm._user_quota_exhausted_date = old_date

        changed = await mm.reset_daily_limits_if_new_day()
        assert changed is True
        assert "model-A" not in mm._disabled
        assert mm._user_quota_exhausted is False
        assert mm._user_quota_exhausted_date is None

    @pytest.mark.asyncio
    async def test_get_status_returns_info(self) -> None:
        mm = ModelManager(reserve=3)
        await mm.mark_disabled("model-A", "test")
        status = await mm.get_status()
        assert status["user_quota_exhausted"] is False
        assert status["disabled_today"] == 1
        assert status["cooldown_count"] == 0
        assert status["quota_reserve"] == 3
        assert len(status["disabled_list"]) == 1
        assert status["disabled_list"][0]["id"] == "model-A"
