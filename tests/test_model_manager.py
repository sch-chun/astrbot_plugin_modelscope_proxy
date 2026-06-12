import pytest
from datetime import date, timedelta
from proxy.model_manager import ModelManager, HEADER_MODEL_REMAINING, HEADER_MODEL_LIMIT
from unittest.mock import MagicMock


class TestModelManager:
    """模型管理器的单元测试——纯内存测试，无网络依赖"""
    
    def test_init(self):
        mm = ModelManager(["model-A", "model-B", "model-C"])
        assert mm._models == ["model-A", "model-B", "model-C"]
        assert len(mm._disabled) == 0
        assert len(mm._cooldown) == 0
    
    @pytest.mark.asyncio
    async def test_get_current_model_returns_first_available(self):
        mm = ModelManager(["model-A", "model-B"])
        model = await mm.get_current_model()
        assert model == "model-A"
    
    @pytest.mark.asyncio
    async def test_mark_disabled_marks_correctly(self):
        mm = ModelManager(["model-A", "model-B", "model-C"])
        
        await mm.mark_disabled("model-B", "测试禁用")
        
        assert "model-B" in mm._disabled
        model = await mm.get_current_model()
        # 应该跳过 model-B，取下一个可用模型
        assert model in ["model-A", "model-C"]
        assert model != "model-B"
    
    @pytest.mark.asyncio
    async def test_mark_429_first_time_applies_cooldown_only(self):
        mm = ModelManager(["model-A"])
        
        is_disabled = await mm.mark_429("model-A")
        
        assert not is_disabled  # 第一次只是冷却
        assert "model-A" in mm._cooldown
        assert mm._429_count["model-A"] == 1
    
    @pytest.mark.asyncio
    async def test_mark_429_third_time_disables_model(self):
        mm = ModelManager(["model-A"])
        
        await mm.mark_429("model-A")  # 第一次
        await mm.mark_429("model-A")  # 第二次
        is_disabled = await mm.mark_429("model-A")  # 第三次
        
        assert is_disabled
        assert "model-A" in mm._disabled
        assert "model-A" not in mm._cooldown
    
    @pytest.mark.asyncio
    async def test_get_current_model_returns_none_when_all_disabled(self):
        mm = ModelManager(["model-A"])
        await mm.mark_disabled("model-A")
        
        model = await mm.get_current_model()
        assert model is None
    
    @pytest.mark.asyncio
    async def test_check_quota_headers_detects_exhaustion(self):
        mm = ModelManager(["model-A"])
        
        # 模拟响应头：模型剩余额度为 0
        mock_headers = MagicMock()
        mock_headers.get.side_effect = lambda key, default=None: {
            HEADER_MODEL_REMAINING: "0",
            HEADER_MODEL_LIMIT: "100",
        }.get(key, default)
        
        model_exhausted, user_exhausted = await mm.check_quota_headers("model-A", mock_headers)
        
        assert model_exhausted is True
        assert "model-A" in mm._disabled
    
    @pytest.mark.asyncio
    async def test_mark_all_disabled_blocks_all_models(self):
        mm = ModelManager(["model-A", "model-B"])
        
        await mm.mark_all_disabled("测试用户配额用尽")
        
        assert await mm.is_user_quota_exhausted() is True
        assert await mm.get_current_model() is None
    
    @pytest.mark.asyncio
    async def test_reset_429_successfully_clears_counter(self):
        mm = ModelManager(["model-A"])
        await mm.mark_429("model-A")
        assert mm._429_count["model-A"] == 1
        
        await mm.reset_429("model-A")
        
        assert "model-A" not in mm._429_count
    
    @pytest.mark.asyncio
    async def test_reset_daily_limits_clears_expired_disabled(self):
        mm = ModelManager(["model-A", "model-B"])
        old_date = date.today() - timedelta(days=1)
        mm._disabled["model-A"] = old_date
        mm._disabled["model-B"] = date.today()
        
        await mm.reset_daily_limits_if_new_day()
        
        assert "model-A" not in mm._disabled  # 过期的已清除
        assert "model-B" in mm._disabled      # 当日的保留
    
    @pytest.mark.asyncio
    async def test_auto_switch_to_next_after_disabled(self):
        mm = ModelManager(["model-A", "model-B", "model-C"])
        
        # 确认初始为 model-A
        current = await mm.get_current_model()
        assert current == "model-A"
        
        await mm.mark_disabled("model-A")
        
        # 应该自动切换到 model-B
        current = await mm.get_current_model()
        assert current == "model-B"
    
    def test_is_available_internal(self):
        mm = ModelManager(["model-A"])
        
        # 初始可用
        assert mm._is_available("model-A") is True
        
        mm._disabled["model-A"] = date.today()
        # 已禁用，不可用
        assert mm._is_available("model-A") is False
    
    @pytest.mark.asyncio
    async def test_get_status_returns_full_information(self):
        mm = ModelManager(["model-A", "model-B"])
        await mm.mark_disabled("model-B")
        
        status = await mm.get_status()
        
        assert status["total"] == 2
        assert status["active"] == 1
        assert status["disabled_today"] == 1
        assert not status["user_quota_exhausted"]
        assert "disabled_list" in status
        assert "models" in status
