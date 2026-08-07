# ⚠️ 已归档 / DEPRECATED

> **本插件已停止维护，原因如下：**
>
> 2026 年 8 月 5 日起，ModelScope 魔搭社区全面上线「魔粒」积分体系，替代原有的每日固定免费调用次数。API-Inference 的响应头中不再返回 `modelscope-ratelimit-*-remaining` 等额度信息，本插件依赖的**额度监控**功能已无法工作。
>
> 插件的智能回退逻辑（错误驱动模型切换、429 冷却、图片压缩等）可能将移植至 [generic_fallback](https://github.com/sch-chun/astrbot_plugin_generic_fallback) 通用回退代理插件，推荐迁移使用。
>
> 如需继续使用 ModelScope 免费 API，建议直接配置为普通 OpenAI 兼容 Provider。

---

# ModelScope Auto Proxy — AstrBot 插件

将 [ModelScope 魔搭社区](https://modelscope.cn) 的免费大模型 API-Inference 包装成 OpenAI 兼容接口，并自动在多个模型间做故障转移。

> 魔搭社区为每个账号每天提供**大量免费调用次数**（不同模型有各自的额度限制），本插件让你像用一个普通模型一样，自动在多个免费模型之间来回切换、额度用尽后自动跳过、第二天自动恢复。

**简体中文** | [English](README_EN.md) | [Русский](README_RU.md)