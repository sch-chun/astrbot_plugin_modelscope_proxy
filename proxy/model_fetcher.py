"""
模型获取模块 — 从 ModelScope API 获取支持 api-inference 的大语言模型列表，
按参数量从大到小排序，过滤掉不适合编码的模型（图像/视频/多模态/推理专用/基座模型等）。
"""
import re
import httpx
import asyncio

from astrbot.api import logger

# 需要排除的关键词
EXCLUDE_KEYWORDS = [
    "vl", "vision", "image", "video", "audio", "speech",
    "whisper", "tts", "asr", "diffus", "paint", "draw",
    "music", "sdxl", "sd-", "stable-diffusion", "flux",
    "clip", "blip", "llava", "internvl", "cogvlm",
    "glm-4v", "qwen-vl", "yi-vl", "minicpm-v",
    "qvq",
    "embedding", "bge-", "rerank",
    "compassjudger", "judger",
    "xiyansql",
    "gui-owl",
    "ministral",
]

# 需要排除的完整模型 ID
EXCLUDE_MODEL_IDS = {
    "PaddlePaddle/ERNIE-4.5-300B-A47B-PT",
    "PaddlePaddle/ERNIE-4.5-21B-A3B-PT",
    "Qwen/Qwen3-32B",
    "Qwen/Qwen3-14B",
    "Qwen/Qwen3-8B",
    "Qwen/Qwen3-4B",
    "Qwen/Qwen3.5-35B-A3B",
    "Qwen/Qwen3.5-27B",
    "Qwen/Qwen3.5-122B-A10B",
    "Qwen/Qwen3-30B-A3B",
    "Qwen/Qwen3-235B-A22B-Thinking-2507",
    "Qwen/Qwen3-30B-A3B-Thinking-2507",
    "Qwen/Qwen3-Next-80B-A3B-Thinking",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
    "Qwen/QwQ-32B",
    "Qwen/QwQ-32B-Preview",
    "deepseek-ai/DeepSeek-R1-0528",
    "LLM-Research/Llama-4-Maverick-17B-128E-Instruct",
}

# 已知模型参数量映射表
KNOWN_MODEL_PARAMS = {
    "deepseek-ai/DeepSeek-V3.2": (685.0, 37.0),
    "MiniMax/MiniMax-M2.5": (456.0, 45.0),
    "ZhipuAI/GLM-5": (744.0, 40.0),
    "ZhipuAI/GLM-4.7-Flash": (9.0, 9.0),
    "moonshotai/Kimi-K2.5": (1000.0, 60.0),
    "Shanghai_AI_Laboratory/Intern-S1": (107.0, 107.0),
    "Shanghai_AI_Laboratory/Intern-S1-mini": (27.0, 27.0),
    "stepfun-ai/Step-3.5-Flash": (80.0, 22.0),
    "XiaomiMiMo/MiMo-V2-Flash": (17.0, 17.0),
    "meituan-longcat/LongCat-Flash-Lite": (27.0, 27.0),
    "mistralai/Mistral-Large-Instruct-2407": (123.0, 123.0),
    "mistralai/Mistral-Small-Instruct-2409": (22.0, 22.0),
    "LLM-Research/c4ai-command-r-plus-08-2024": (104.0, 104.0),
}


def parse_param_size(model_id: str) -> float:
    """从模型 ID 中提取参数量（单位 B）"""
    if model_id in KNOWN_MODEL_PARAMS:
        total, _ = KNOWN_MODEL_PARAMS[model_id]
        return total

    match = re.search(r"(\d+(?:\.\d+)?)\s*[Bb]", model_id)
    if match:
        return float(match.group(1))

    match = re.search(r"(\d+(?:\.\d+)?)B-A\d+B", model_id, re.IGNORECASE)
    if match:
        return float(match.group(1))

    return 0.0


def is_text_model(model_id: str) -> bool:
    """判断模型是否适合编码场景"""
    model_lower = model_id.lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in model_lower:
            return False
    if model_id in EXCLUDE_MODEL_IDS:
        return False
    return True


async def fetch_models_from_api(api_key: str, base_url: str) -> list[dict]:
    """从 ModelScope API 获取模型列表"""
    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data", [])
            logger.info(f"从 ModelScope API 获取到 {len(models)} 个模型")
            return models
        except httpx.HTTPStatusError as e:
            logger.error(f"获取模型列表失败 (HTTP {e.response.status_code}): {e}")
            return []
        except Exception as e:
            logger.error(f"获取模型列表异常: {e}")
            return []


async def fetch_model_detail(model_id: str) -> dict | None:
    """从 ModelScope hub API 获取模型详细信息"""
    url = f"https://modelscope.cn/api/v1/models/{model_id}"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json().get("data", {})
        except Exception:
            pass
    return None


def estimate_param_from_storage(storage_size: int) -> float:
    """从模型存储大小估算参数量（单位 B）"""
    if storage_size <= 0:
        return 0.0
    estimated_b = storage_size / 1.5 / 1e9
    return round(estimated_b, 1)


async def get_filtered_models(api_key: str, base_url: str, min_param_b: int = 4) -> list[dict]:
    """获取过滤后的文本大模型列表，按参数量从大到小排序"""
    raw_models = await fetch_models_from_api(api_key, base_url)
    if not raw_models:
        logger.warning("未获取到任何模型，返回空列表")
        return []

    # 找出需要查详情的模型
    models_need_detail = []
    for m in raw_models:
        model_id = m.get("id", "")
        if not model_id:
            continue
        if not is_text_model(model_id):
            continue
        if model_id not in KNOWN_MODEL_PARAMS and parse_param_size(model_id) == 0:
            models_need_detail.append(model_id)

    # 批量获取未知模型的详细信息
    storage_map: dict[str, int] = {}
    if models_need_detail:
        logger.info(f"需要从 hub API 获取参数量的模型: {len(models_need_detail)} 个")
        detail_tasks = [fetch_model_detail(mid) for mid in models_need_detail]
        details = await asyncio.gather(*detail_tasks, return_exceptions=True)
        for mid, detail in zip(models_need_detail, details):
            if isinstance(detail, dict) and detail.get("StorageSize"):
                storage_map[mid] = detail["StorageSize"]

    filtered = []
    for m in raw_models:
        model_id = m.get("id", "")
        if not model_id or not is_text_model(model_id):
            continue

        param_b = parse_param_size(model_id)
        if param_b == 0:
            if model_id in storage_map:
                param_b = estimate_param_from_storage(storage_map[model_id])
                logger.info(f"从存储大小估算参数量: {model_id} -> {param_b}B")
            else:
                param_b = 10.0
                logger.info(f"无法获取参数量，使用默认值: {model_id} -> {param_b}B")

        if param_b < min_param_b:
            logger.debug(f"排除小模型 (<{min_param_b}B): {model_id} ({param_b}B)")
            continue

        filtered.append({
            "id": model_id,
            "param_b": param_b,
            "owned_by": m.get("owned_by", "unknown"),
            "created": m.get("created", 0),
        })

    filtered.sort(key=lambda x: x["param_b"], reverse=True)
    logger.info(f"过滤后保留 {len(filtered)} 个文本大模型 (>= {min_param_b}B)")
    return filtered
