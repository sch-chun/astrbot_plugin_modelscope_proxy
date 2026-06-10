# ModelScope Auto Proxy — AstrBot 插件

将 [ModelScope 魔搭社区](https://modelscope.cn) 的免费大模型 API-Inference 包装成 OpenAI 兼容接口，并自动在多个模型间做故障转移。

> 魔搭社区为每个账号每天提供**大量免费调用次数**（不同模型有各自的额度限制），本插件让你像用一个普通模型一样，自动在多个免费模型之间来回切换、额度用尽后自动跳过、第二天自动恢复。

## 功能特性

- **🧩 AstrBot 插件** — 即装即用，在 AstrBot 管理面板中配置后即可使用
- **🔄 自动故障转移** — 模型请求失败（HTTP 5xx、超时、连接错误等）自动切换到下一个模型
- **📊 基于响应头的额度控制** — 通过 `modelscope-ratelimit-*-remaining` 响应头检测额度，额度用尽提前禁用，不等 429
- **⏱ 每日自动重置** — 每日零点自动清除禁用记录，恢复所有模型
- **🚦 流式/非流式都支持** — SSE 流式传输完整可用
- **📎 可选的模型标记** — 开启后每个回复自动注入当前使用的模型名，方便排查

## 快速开始

### 1. 安装插件

在 AstrBot 管理面板的「插件市场」中安装，或将本仓库克隆到 `AstrBot/data/plugins/` 目录下。

### 2. 获取 API Key

前往 [ModelScope 我的令牌](https://modelscope.cn/my/myaccesstoken) 创建一个 Access Token。

### 3. 配置插件

在 AstrBot 管理面板的「插件配置」中填写：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `modelscope_api_key` | string | `""` | 你的 ModelScope Access Token |
| `model_list` | list | `[]` | 模型回退列表，按优先级排序。至少配一个 |
| `proxy_port` | int | `3473` | 代理服务监听端口 |
| `virtual_model_name` | string | `"modelscope-auto"` | 对外暴露的虚拟模型名 |
| `show_model_tag` | bool | `false` | 是否在回复中注入 `[模型名]` 标记 |

**`model_list` 配置示例：**

```json
[
  "Qwen/Qwen3-Coder-480B",
  "Qwen/Qwen3.5-397B",
  "Qwen/Qwen3-393B",
  "Qwen/Qwen3-235B-A22B"
]
```

> 按优先级从上到下排列，前面的模型优先使用。额度用尽或不可用时自动切换到下一个。

### 4. 在 AstrBot 中添加模型提供商

插件启动后，需要在 AstrBot 管理面板中配置模型提供商，让 AstrBot 能通过本插件调用模型：

1. 进入管理面板 → **模型提供商**
2. 添加一个 **OpenAI API 兼容** 的提供商
3. 填写以下参数：

| 参数 | 值 |
|------|-----|
| API URL | `http://127.0.0.1:3473/v1`（端口与你配置的 `proxy_port` 一致） |
| API Key | **随便填一个非空字符串**（插件不校验 Key，但不能留空，否则 AstrBot 会报错） |
| 模型名 | modelscope-auto（与 `virtual_model_name` 一致） |

> API Key 随便填比如 `sk-placeholder` 就行了，只要不空就能用。

### 5. 开始使用

配置完成后，在 AstrBot 中就可以像用普通 OpenAI 模型一样使用 ModelScope 的免费模型了。对话时 AstrBot 会调用本插件，插件自动按优先级选择可用模型转发请求。

```bash
# 也可以直接用 curl 测试
curl http://127.0.0.1:3473/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "modelscope-auto",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

**在 OpenAI SDK 中使用：**

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-placeholder",  # 随便填，插件不校验
    base_url="http://127.0.0.1:3473/v1"
)

response = client.chat.completions.create(
    model="modelscope-auto",
    messages=[{"role": "user", "content": "你好呀！"}],
    stream=True
)
```

## 路由逻辑说明

```
用户请求 → 取当前可用模型（按优先级）
         ↓
      转发到 ModelScope API
         ↓
    ┌────┴────┐
    │ 请求成功 │  →  检查响应头：额度够 → 返回结果
    └────┬────┘     额度用尽 → 标记禁用，切下一个
         ↓
    ┌────┴────┐
    │ 请求失败 │  →  5xx/超时 → 标记禁用，切下一个
    └─────────┘     429 → 计数，连续 3 次 → 标记禁用
                     400 → 冷却 5 分钟，切下一个
```

- **额度用尽**：每天零点自动解禁所有模型
- **短时冷却**：400 错误冷却 5 分钟，429 首次短冷却 2 分钟
- **用户总配额用尽** (`modelscope-ratelimit-requests-remaining: 0`)：所有模型直接禁用，次日恢复

## API 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容的聊天补全（流式/非流式） |
| `/v1/models` | GET | 返回虚拟模型列表 |
| `/v1/status` | GET | 插件内部状态（各模型启用/禁用/冷却情况） |

## 开发

```bash
# 安装依赖
pip install -r requirements.txt
```

## License

MIT
