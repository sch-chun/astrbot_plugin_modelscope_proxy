# ModelScope Auto Proxy — AstrBot Plugin

Wraps the free LLM API-Inference endpoints from [ModelScope](https://modelscope.cn) into an OpenAI-compatible interface, with automatic failover across multiple models.

> ModelScope provides **a large number of free daily API calls** for each account (limits vary by model). This plugin lets you use them as if they were a single model — automatically rotating between models, skipping those that run out of quota, and recovering them the next day.

---

## Features

- **🧩 AstrBot Plugin** — Ready to use out of the box; configure it directly in the AstrBot admin panel.
- **🔄 Multi Virtual Model Support** — Expose multiple virtual models simultaneously, each with its own fallback list and optional failover provider.
- **🔁 Automatic Failover** — On failure (HTTP 5xx, timeout, connection error, etc.), the request is automatically retried on the next model.
- **📊 Response Header-Based Quota Control** — Detects remaining quota from the `modelscope-ratelimit-*-remaining` response headers, proactively disabling models before they return HTTP 429.
- **🛡️ Global Quota Reserve** — Set `global_quota_reserve` to stop all calls when remaining quota falls to or below that value, protecting other services (e.g., text-to-image).
- **🔒 API Key Authentication** — Optionally set an API key for the proxy itself to prevent unauthorized access.
- **🌐 Configurable Listen Address** — Defaults to `127.0.0.1`; can be changed to `0.0.0.0` for external access.
- **📈 Monitoring Dashboard** — Built-in WebUI that visualizes user quota and per-model status.
- **⏱ Daily Auto Reset** — All disabled models are re-enabled automatically at midnight.
- **🚦 Full Streaming Support** — SSE streaming works seamlessly.

---

## Quick Start

### 1. Install the Plugin

In the AstrBot admin panel, go to **Plugin Market**, search for `Modelscope Proxy`, and install it.

### 2. Get a ModelScope API Key

Go to [ModelScope Access Tokens](https://modelscope.cn/my/myaccesstoken) and create an Access Token.

> **Note:** After registration, you must link an Alibaba Cloud account and complete real-name verification before you can use API-Inference.

### 3. Configure the Plugin

Fill in the configuration in the AstrBot admin panel under **Plugin Settings** (v0.3.0+ uses the new multi virtual model format):

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `modelscope_api_key` | string | `""` | Your ModelScope Access Token (required) |
| `proxy_port` | int | `3473` | Port the proxy server listens on |
| `proxy_host` | string | `"127.0.0.1"` | Listen address (keep as localhost, or set to `0.0.0.0` for external access) |
| `proxy_api_key` | string | `""` | API key for the proxy itself (optional; when set, clients must include `Authorization: Bearer <key>`) |
| `log_response` | bool | `false` | Enable to log upstream response bodies in debug mode |
| `global_quota_reserve` | int | `0` | Global quota reserve (calls); stops all ModelScope requests when remaining ≤ this value, protecting other services |
| `virtual_models` | template_list | `[]` | **Virtual model configuration list** (core config; see below) |

**`virtual_models` configuration:**

Each virtual model is a template with the following fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | `"modelscope-auto"` | Exposed virtual model name |
| `model_list` | list | `[]` | ModelScope model fallback list in priority order (at least one required) |
| `fallback` | string | `""` | Optional fallback provider config, used when all ModelScope models are unavailable |

**`fallback` field notes:**

When all ModelScope models are unavailable, requests are forwarded to a configured OpenAI-compatible provider within AstrBot.

Must be an external provider (i.e., the `api_base` must not point to the proxy itself), otherwise it will be automatically disabled.

Must be an OpenAI-compatible provider; otherwise forwarding is not possible.

**Example configuration:**

```json
[
  {
    "name": "qwen-auto",
    "model_list": [
      "Qwen/Qwen3-Coder-480B",
      "Qwen/Qwen3.5-397B",
      "Qwen/Qwen3-393B"
    ],
    "fallback": ""
  },
  {
    "name": "qwen-fallback",
    "model_list": ["Qwen/Qwen3-235B-A22B"],
    "fallback": "openai/gpt-4o"
  }
]
```

Starting from v0.3.1, the plugin ships with three default virtual models and their fallback lists:

- **modelscope-auto**: Higher-capability models
- **modelscope-auto-weak**: Lower-capability models
- **modelscope-auto-vision**: Vision / multimodal models

> The default capability ranking may not be rigorous. Feel free to adjust it or open an issue if you find discrepancies. Due to the instability of the ModelScope API, some models may be unavailable or suboptimal — adding or removing them is expected and encouraged.

> ⚠️ If `fallback` is not configured, requests will return HTTP 503 when all ModelScope models are unavailable.

### 4. Add a Model Provider in AstrBot

Once the plugin is running, configure a model provider in the AstrBot admin panel:

1. Open the admin panel → **Model Providers**
2. Add an **OpenAI API Compatible** provider
3. Fill in the parameters:

| Parameter | Value |
|-----------|-------|
| API URL | `http://127.0.0.1:3473/v1` (match the port with `proxy_port`) |
| API Key | If `proxy_api_key` is set, use that value; otherwise put any placeholder (cannot be blank) |
| Model Name | Click "Fetch Model List" to auto-populate available virtual models (or enter manually) |

### 5. Start Using

Once configured, you can use it in AstrBot just like any normal OpenAI model. The plugin will automatically select the best available model by priority.

```bash
# Test directly with curl
curl http://127.0.0.1:3473/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-auto",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

**Using with the OpenAI SDK:**

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-placeholder",  # Use your proxy_api_key value if configured
    base_url="http://127.0.0.1:3473/v1"
)

response = client.chat.completions.create(
    model="qwen-auto",          # Use the virtual model name you configured
    messages=[{"role": "user", "content": "Hello there!"}],
    stream=True
)
```

---

## Monitoring Dashboard

The plugin includes a built-in WebUI monitoring page, accessible from the AstrBot admin panel's plugin detail page (page name: `monitor`). The dashboard displays:

- **User global quota**: current remaining / total, with a progress bar that changes color based on the remaining ratio
- **Per-virtual-model ModelScope model lists**:
  - Model ID
  - Status (available / disabled / cooldown)
  - Current remaining quota (from the last response header)
- **Auto-refresh**: updates every 30 seconds; manual refresh also available

> **Note:** Quota data is only updated when actual requests are made. If no requests have been made today, the total quota and per-model quotas will show "Not yet fetched."

---

## Routing Logic

```
User request (specifying a virtual model name)
              ↓
      Match virtual model config
              ↓
   ┌─────┴─────┐
   │ User Quota │  If remaining ≤ global_quota_reserve, go directly to fallback or return 503
   └─────┬─────┘
              ↓
   Pick the first available model from model_list (by priority)
              ↓
       Forward to ModelScope API
              ↓
    ┌────┴────┐
    │ Success │  →  Check response headers: quota OK → return result
    └────┬────┘     quota exhausted → mark disabled, switch to next
         ↓
    ┌────┴────┐
    │ Failure │  →  5xx/timeout → mark disabled, switch to next
    └─────────┘     429 → count; 3 consecutive → mark disabled
                    400 → cooldown 5 min, switch to next
         ↓
   All ModelScope models failed?
         ↓
   ┌────┴────┐
   │ fallback │  →  Call fallback and return result
   └────────────┘
         ↓
   No service available → return 503
```

- **Quota exhausted**: All models are re-enabled automatically at midnight daily.
- **Short cooldown**: HTTP 400 triggers a 5-minute cooldown; HTTP 429 triggers a 2-minute short cooldown on first occurrence.
- **Global quota reserve**: When `modelscope-ratelimit-requests-remaining ≤ global_quota_reserve`, global disable is triggered — no further ModelScope requests are made until the next day's reset.

---

## API Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/v1/chat/completions` | POST | OpenAI-compatible chat completion (streaming & non-streaming) |
| `/v1/models` | GET | Returns the list of all virtual models |
| `/v1/status` | GET | Internal plugin status (disabled/cooldown state of each model) |
| `/v1/quota_status` | GET | Detailed quota status (for the monitoring dashboard) |

> **Note:** If `proxy_api_key` is configured, all endpoints require a Bearer token in the `Authorization` header.

---

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests
pytest tests/
```

---

## License

AGPL-3.0

---

## Acknowledgements

Prototype inspiration: [ModelScope Auto Proxy](https://github.com/comedy1024/modelscope-auto-proxy)

Platform support: [AstrBot](https://github.com/AstrBotDevs/AstrBot)