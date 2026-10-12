# ISSUE-015：项目生命周期与依赖弃用警告

- 整理日期：2026-10-11
- 记录状态：历史修复已验证；上游警告保留
- 范围：问题复盘；下方测试结果均为原记录中的历史证据，本次未重跑。
- 当前实现、验收及交付状态：[README](../../README.md)与[CHG-003](../changes/CHG-003-planning-performance-observability.md)；历史v3范围另见[CHG-002](../changes/CHG-002-trip-plan-enhancements.md)。

## 1. 问题描述

旧Pydantic/FastAPI用法、Authlib兼容及测试连接未关闭产生启动警告。

## 2. 排查过程与原因判断

以下迁移原学习日志“后端启动弃用警告修复（2026-10-03）”的完整问题记录。带日期的实现、调用、测试和Git状态是历史事实；后续变更以当前状态入口为准。原记录未列出的复现步骤或尝试不补造。

<details>
<summary>排查、实现与验证证据</summary>

- 原因：项目仍使用 Pydantic 的 `class Config`、旧示例参数和 FastAPI `on_event`；Authlib 1.8 提示安装 HTTPX2，FastMCP 2 内部仍导入已弃用的 JOSE；旧库迁移测试的 SQLite 连接未显式关闭。
- 修改：配置迁移到 `model_config`，字段及查询参数使用 `examples`，应用启停迁移到 `lifespan`；补充并安装 HTTPX2，保留 HelloAgents 所需的 httpx，将 FastMCP 限定为兼容的 2.x、Authlib 限定为 2.0 之前。JOSE 提示只按上游模块、类别和消息精确过滤，其他警告及运行异常保留。迁移测试显式关闭数据库连接。
- 验证：新增真实应用生命周期测试，覆盖临时库初始化、健康及任务接口、退出关闭服务和无效配置拒绝启动；后端 88 项测试全部通过，日志没有本次列出的警告；项目弃用警告按错误处理时，应用导入和 OpenAPI 生成通过。现有第三方 HelloAgents 自身的旧式模型配置未在本次修改中迁移，因此不宣称全部依赖在强制显示所有弃用警告时均无警告。
- 注意：验证使用临时数据库，不启动或停止用户的 VS Code 服务；已运行的后端需要重启才能加载修改。JOSE 的完整迁移需等待兼容 HelloAgents 的上游版本。
- 兼容性复查：高德 MCP 仍成功发现 16 个工具；该独立子进程的 uv 缓存依赖另有 `IncompleteFieldDefinitionWarning`（`lifespan` 类型前向引用），与本次后端启动警告不同，尚未修改上游缓存或过滤此提示。

</details>

## 3. 尝试方案与方案结果

迁移model_config/examples/lifespan，约束兼容依赖并精确过滤上游JOSE提示；88项测试通过。

## 4. 解决结果与验证边界

项目范围警告改善；HelloAgents和独立MCP的其他警告未全部处理。

## 5. 经验总结

逐个识别警告来源，不能用全局过滤替代兼容性修复。

## 6. 证据来源

- [学习日志时间线](../learning-log.md)：保留原章节入口，问题正文已迁至本文。
- 原文列出的代码、测试、提交和专项验收见上方证据。临时路径仅定位历史材料，不保证文件仍存在。
