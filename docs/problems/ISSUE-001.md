# ISSUE-001：Vite 客户端类型声明缺失

- 整理日期：2026-10-11
- 记录状态：历史修复已验证
- 范围：问题复盘；下方测试结果均为原记录中的历史证据，本次未重跑。
- 当前实现、验收及交付状态：[README](../../README.md)与[CHG-003](../changes/CHG-003-planning-performance-observability.md)；历史v3范围另见[CHG-002](../changes/CHG-002-trip-plan-enhancements.md)。

## 1. 问题描述

前端构建无法识别 CSS 副作用导入及 import.meta.env。

## 2. 排查过程与原因判断

以下迁移原学习日志“问题 001：Vite 类型环境缺失”的完整问题记录。带日期的实现、调用、测试和Git状态是历史事实；后续变更以当前状态入口为准。原记录未列出的复现步骤或尝试不补造。

<details>
<summary>排查、实现与验证证据</summary>

#### 1. 目标

让 TypeScript 正确识别 Vite 提供的客户端类型，并减少当前前端构建错误。

#### 2. 错误现象

执行：

```bash
cd frontend
npm run build
```

关键报错包括：

```text
Cannot find module 'ant-design-vue/dist/reset.css' or its corresponding type declarations.
Property 'env' does not exist on type 'ImportMeta'.
```

同一次构建还暴露了日期字段类型冲突和未使用函数；它们不属于 Vite 类型环境问题，已在对应组件中一并修复。

#### 3. 原因分析

`tsconfig.json` 只包含项目源码，并不会自动让 TypeScript 加载 Vite 的客户端全局声明。

Vite 的 `vite/client` 声明文件提供两类项目需要的环境类型：

- `declare module '*.css'`，使 `import 'ant-design-vue/dist/reset.css'` 成为合法的副作用导入；
- `ImportMeta` 与 `ImportMetaEnv`，使 `import.meta.env.VITE_*` 可以通过类型检查。

项目启用了 `noUncheckedSideEffectImports`，因此缺少 CSS 声明时会直接报 `TS2307`；缺少 `ImportMeta` 扩展时会报 `TS2339`。

#### 4. 尝试过的方案

未采用在每个组件中手写 `declare module '*.css'` 或把 `import.meta` 强制转换为 `any` 的方式。前者会重复维护声明，后者会失去环境变量的自动补全和类型检查。

#### 5. 最终解决方案

新增 `frontend/src/vite-env.d.ts`：

```ts
/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_AMAP_WEB_JS_KEY: string
}
```

三斜线指令会让 TypeScript 加载 Vite 的客户端声明；同名接口会通过声明合并补充本项目的 `VITE_*` 变量。`ImportMeta` 中的 `env` 字段由 `vite/client` 提供，本项目的声明只约束具体变量。

#### 6. 验证方法

在 `frontend` 目录执行：

```bash
npm run build
```

实际结果：`vue-tsc && vite build` 通过，CSS 导入和 `import.meta.env` 的类型错误均已消除。

附带修复：日期选择器使用 `Dayjs | null`，API 请求使用 `YYYY-MM-DD` 字符串。表单状态通过 `Omit<TripFormData, 'start_date' | 'end_date'>` 替换日期字段类型，并在提交时调用 `format()` 转换，避免交叉类型产生错误的 `string & Dayjs`。

#### 7. 注意事项

`VITE_*` 变量在 Vite 构建时注入前端代码，不应放置任何服务器端密钥。`VITE_AMAP_WEB_JS_KEY` 是浏览器使用的高德 Web 端 JS Key，与后端的 `AMAP_API_KEY` 不同。

类型声明只能帮助开发阶段发现拼写和使用错误，不能证明变量在运行时已配置。因此地图初始化额外检查空值和 `your_` 占位符，并提示用户在 `frontend/.env` 中配置后重启前端服务。

#### 8. 学习总结

遇到 `import.meta.env` 或 CSS 导入的类型错误时，先检查项目是否存在并被 `tsconfig.json` 包含的 `vite-env.d.ts`，再确认其中包含 `/// <reference types="vite/client" />`。随后只为项目实际使用的 `VITE_*` 变量补充类型，并在依赖配置的功能入口增加运行时校验。

---

</details>

## 3. 尝试方案与方案结果

加载 vite/client 并声明实际环境变量；历史 npm run build 通过。手写重复声明或 any 为未采用方案，不是失败实验。

## 4. 解决结果与验证边界

已消除当次类型错误；运行时配置仍须单独检查。

## 5. 经验总结

先检查环境类型是否被 tsconfig 包含，再改组件类型。

## 6. 证据来源

- [学习日志时间线](../learning-log.md)：保留原章节入口，问题正文已迁至本文。
- 原文列出的代码、测试、提交和专项验收见上方证据。临时路径仅定位历史材料，不保证文件仍存在。
