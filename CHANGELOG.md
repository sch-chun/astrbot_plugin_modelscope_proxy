# Changelog

All notable changes to this project will be documented in this file.

## [0.3.2] - 2026-06-25

### Changed
- 请求超时不再禁用全天

## [0.3.1] - 2026-06-21

### Changed
- ModelScope 阵发性返回 null choice，原因不明。插件目前遇到该返回后会标记为冷却进行回退处理

## [0.3.0] - 2026-06-21

### Added
- **多虚拟模型配置**：使用 `template_list` 类型的 `virtual_models` 配置，支持同时暴露多个虚拟模型，每个模型拥有独立的回退列表和可选的兜底模型（Fallback）
- **全局额度保留值**：新增 `global_quota_reserve` 配置项，当用户剩余额度 ≤ 该值时提前禁用所有调用，避免影响其他服务（如文生图）
- **代理服务 API Key 验证**：新增 `proxy_api_key` 配置，保护代理服务自身免受未授权访问
- **可配置监听地址**：新增 `proxy_host` 配置（默认 `127.0.0.1`），支持绑定到特定 IP，提高安全性
- **启动时模型校验**：初始化时自动请求 ModelScope 的 `/v1/models` 接口，验证配置的模型是否有效，自动移除无效模型并记录警告
- **监控页面**：新增插件 Pages 监控面板，可视化展示用户全局额度、各虚拟模型下每个 ModelScope 模型的剩余额度和状态（可用/禁用/冷却）
- **单元测试全面覆盖**：重写并新增测试用例，覆盖多虚拟模型、兜底、API Key 验证、全局保留值、日志开关等特性

### Changed
- **配置结构重构**：移除原有的 `virtual_model_name` 和 `model_list`，改用 `virtual_models` 模板列表，支持更灵活的模型编排
- **ModelManager 重构**：移除内部模型列表和索引，改为纯状态管理器，由上层调用方按优先级查询可用模型
- **api_proxy 重构**：适配多虚拟模型和兜底逻辑，优化错误处理流程
- **日志输出优化**：调整部分日志级别和格式，便于调试

### Fixed
- 修复插件退出时 `Event loop is closed` 异常，优化 `close_http_client` 的事件循环检查
- 修复部分响应日志中 `NoneType` 访问问题，增加安全检查

### Removed
- 移除 `virtual_model_name` 和 `model_list` 配置项（已被 `virtual_models` 取代）

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
