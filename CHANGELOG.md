# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-06-19

### Added
- 新增 `log_response` 配置开关，开启后会将上游响应内容打印到日志（调试用）
  - 非流式请求：输出完整 JSON 响应，提取 `choices[].message.content` 单独展示
  - 流式请求：收集所有 `delta.content` 片段，拼接完整文本后输出

### Fixed
- 修复响应日志中 `NoneType` 的 TypeError 错误
  - 添加对 `resp_content` 的 None 安全检查
  - 增强对 `choices` 字段的类型校验，防止上游返回 `null` 时崩溃

## [0.1.0] - 2026-06-10

### Initial Release
- 基于 modelscope_auto_proxy 移植为 AstrBot 插件
- 支持自定义模型回退列表（按优先级排序）
- 支持可配置的代理服务端口和虚拟模型名
- 支持在回复文本头部注入 `[模型名]` 标识
- 自动故障切换和智能限速处理
- 基于响应头的额度检测机制
- 每日额度自动重置
