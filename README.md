# ModelScope Auto Proxy — AstrBot 插件

将 [ModelScope 魔搭社区](https://modelscope.cn) 的免费大模型 API-Inference 包装成 OpenAI 兼容接口，并自动在多个模型间做故障转移。

> 魔搭社区为每个账号每天提供**大量免费调用次数**（不同模型有各自的额度限制），本插件让你像用一个普通模型一样，自动在多个免费模型之间来回切换、额度用尽后自动跳过、第二天自动恢复。

---

## 功能特性

- **🧩 AstrBot 插件** — 即装即用，在 AstrBot 管理面板中配置后即可使用
- **🔄 多虚拟模型支持** — 可同时暴露多个虚拟模型，每个模型拥有独立的回退列表和可选的兜底 API
- **🔁 自动故障转移** — 模型请求失败（HTTP 5xx、超时、连接错误等）自动切换到下一个模型
- **📊 基于响应头的额度控制** — 通过 `modelscope-ratelimit-*-remaining` 响应头检测额度，额度用尽提前禁用，不等 429
- **🛡️ 全局额度保留** — 设置 `global_quota_reserve`，当剩余额度 ≤ 该值时停止调用，避免影响其他服务（如文生图）
- **🔒 API Key 验证** — 可选为代理服务自身设置 API Key，防止未授权访问
- **🌐 可配置监听地址** — 默认仅监听 `127.0.0.1`，可修改为 `0.0.0.0` 实现外部访问
- **📈 监控面板** — 内置 WebUI 页面，可视化展示用户额度及各模型状态
- **⏱ 每日自动重置** — 每日零点自动清除禁用记录，恢复所有模型
- **🚦 流式/非流式都支持** — SSE 流式传输完整可用
- **📎 可选的模型标记** — 开启后每个回复自动注入当前使用的模型名，方便排查

---

## 快速开始

### 1. 安装插件

在 AstrBot 管理面板的「AstrBot 插件」中通过 GitHub 链接安装，或将本仓库克隆到 `AstrBot/data/plugins/` 目录下。

### 2. 获取 ModelScope API Key

前往 [ModelScope 我的令牌](https://modelscope.cn/my/myaccesstoken) 创建一个 Access Token。
· 注意: 账号注册后需绑定阿里云账号，并且通过实名认证后才可使用 API-Inference。

### 3. 配置插件

在 AstrBot 管理面板的「插件配置」中填写（支持多虚拟模型，v0.3.0 起使用新配置格式）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `modelscope_api_key` | string | `""` | 你的 ModelScope Access Token（必填） |
| `proxy_port` | int | `3473` | 代理服务监听端口 |
| `proxy_host` | string | `"127.0.0.1"` | 代理服务监听地址（建议保持本地，如需外部访问改为 `0.0.0.0`） |
| `proxy_api_key` | string | `""` | 代理服务自身 API Key（可选，设置后客户端需在 `Authorization: Bearer <key>` 中携带） |
| `show_model_tag` | bool | `false` | 是否在回复中注入 `[模型名]` 标记 |
| `log_response` | bool | `false` | 调试时开启，将上游响应内容打印到日志 |
| `global_quota_reserve` | int | `0` | 全局额度保留值（次），剩余 ≤ 该值时停止调用，保护其他服务 |
| `virtual_models` | template_list | `[]` | **虚拟模型配置列表**（核心配置，见下方说明） |

**`virtual_models` 配置说明：**

每个虚拟模型是一个模板，包含以下字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | string | `"modelscope-auto"` | 对外暴露的虚拟模型名称 |
| `model_list` | list | `[]` | ModelScope 模型回退列表，按优先级排序（至少一个） |
| `fallback` | object | `{}` | 可选的兜底模型配置，当所有 ModelScope 模型不可用时使用 |

**`fallback` 字段说明：**
- `api_key`（string）：兜底 API 的密钥
- `base_url`（string）：兜底 API 的 base URL（OpenAI 兼容），默认 `"https://api.openai.com/v1"`
- `model_name`（string）：兜底模型名称，默认 `"gpt-3.5-turbo"`

**配置示例：**

```json
[
  {
    "name": "qwen-auto",
    "model_list": [
      "Qwen/Qwen3-Coder-480B",
      "Qwen/Qwen3.5-397B",
      "Qwen/Qwen3-393B"
    ],
    "fallback": {}
  },
  {
    "name": "qwen-fallback",
    "model_list": ["Qwen/Qwen3-235B-A22B"],
    "fallback": {
      "api_key": "sk-your-openai-key",
      "base_url": "https://api.openai.com/v1",
      "model_name": "gpt-4o-mini"
    }
  }
]
```

> ⚠️ 如果不配置 `fallback`，当所有 ModelScope 模型不可用时，请求将返回 503。

### 4. 在 AstrBot 中添加模型提供商

插件启动后，在 AstrBot 管理面板中配置模型提供商：

1. 进入管理面板 → **模型提供商**
2. 添加一个 **OpenAI API 兼容** 的提供商
3. 填写参数：

| 参数 | 值 |
|------|-----|
| API URL | `http://127.0.0.1:3473/v1`（端口与 `proxy_port` 一致） |
| API Key | 如果配置了 `proxy_api_key`，填对应的值；否则随便填（不能留空） |
| 模型名 | 点击获取模型列表将自动返回可用虚拟模型（或手动填写） |

### 5. 开始使用

配置完成后，在 AstrBot 中就可以像用普通 OpenAI 模型一样使用了。对话时插件会自动按优先级选择可用模型。

```bash
# 直接用 curl 测试
curl http://127.0.0.1:3473/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-auto",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

**在 OpenAI SDK 中使用：**

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-placeholder",  # 若配置了 proxy_api_key，则填对应的值
    base_url="http://127.0.0.1:3473/v1"
)

response = client.chat.completions.create(
    model="qwen-auto",          # 使用你配置的虚拟模型名
    messages=[{"role": "user", "content": "你好呀！"}],
    stream=True
)
```

---

## 监控面板

插件内置了一个 WebUI 监控页面，在 AstrBot 管理面板的插件详情页中可打开（页面名称为 `monitor`）。面板展示：

- **用户全局额度**：当前剩余次数/总次数，进度条颜色随剩余比例变化
- **各虚拟模型下的 ModelScope 模型列表**：
  - 模型 ID
  - 状态（可用/禁用/冷却中）
  - 当前剩余额度（来自最后一次响应头）
- **自动刷新**：每 30 秒自动更新，也可手动刷新

> 注意：额度数据仅在实际请求返回时更新，若长时间无请求，部分模型可能显示“未获取”。

---

## 路由逻辑说明

```
用户请求（指定虚拟模型名）
         ↓
    匹配虚拟模型配置
         ↓
   ┌─────┴─────┐
   │ 用户总额度 │  如果剩余 ≤ global_quota_reserve，直接走 fallback 或返回 503
   └─────┬─────┘
         ↓
  取 model_list 中第一个可用模型（按优先级）
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
         ↓
  所有 ModelScope 模型失败？
         ↓
   ┌────┴────┐
   │ 有 fallback │  →  调用 fallback 返回结果
   └────────────┘
         ↓
  无可用服务 → 返回 503
```

- **额度用尽**：每天零点自动解禁所有模型
- **短时冷却**：400 错误冷却 5 分钟，429 首次短冷却 2 分钟
- **全局额度保留**：当 `modelscope-ratelimit-requests-remaining ≤ global_quota_reserve` 时，触发全局禁用，不再发起任何 ModelScope 请求（直至次日重置）

---

## API 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容的聊天补全（流式/非流式） |
| `/v1/models` | GET | 返回所有虚拟模型列表 |
| `/v1/status` | GET | 插件内部状态（各模型禁用/冷却情况） |
| `/v1/quota_status` | GET | 详细的额度状态（供监控页面使用） |

> 注意：如果配置了 `proxy_api_key`，所有端点都需要在 `Authorization` 头中携带 Bearer Token。

---

## 开发

```bash
# 安装依赖
pip install -r requirements.txt

# 运行测试
pytest tests/
```

---

## License

AGPL-3.0 license

---

## 致谢

原型启发：[ModelScope Auto Proxy](https://github.com/comedy1024/modelscope-auto-proxy)
平台支持：[AstrBot](https://github.com/AstrBotDevs/AstrBot)
