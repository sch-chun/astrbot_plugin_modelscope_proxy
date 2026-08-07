# ⚠️ DEPRECATED / 已归档

> **This plugin is no longer maintained. Reason:**
>
> Since August 5, 2026, ModelScope has rolled out the "魔粒" (Magic Beans) credit system, replacing the previous daily fixed free call quota. The API-Inference response headers no longer return `modelscope-ratelimit-*-remaining` quota information. The **quota monitoring** functionality that this plugin depends on is no longer functional.
>
> The intelligent fallback logic (error-driven model switching, 429 cooldown, image compression, etc.) has been ported to the [generic_fallback](https://github.com/sch-chun/astrbot_plugin_generic_fallback) universal fallback proxy plugin. Migration is recommended.
>
> To continue using ModelScope's free API, consider configuring it as a regular OpenAI-compatible Provider. For higher stability, consider commercial APIs such as [Alibaba Cloud Bailian](https://www.aliyun.com/product/bailian).

---

# ModelScope Auto Proxy — AstrBot Plugin

Wraps the free LLM API-Inference endpoints from [ModelScope](https://modelscope.cn) into an OpenAI-compatible interface, with automatic failover across multiple models.

> ModelScope provides **a large number of free daily API calls** for each account (limits vary by model). This plugin lets you use them as if they were a single model — automatically rotating between models, skipping those that run out of quota, and recovering them the next day.

[简体中文](README.md) | **English** | [Русский](README_RU.md)