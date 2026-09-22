# AGENTS.md — HelloAgents智能旅行助手

AI旅行规划助手：用户在 `frontend` 填写目的地/日期/偏好，后端用 HelloAgents 的 `SimpleAgent` 通过高德地图（amap）MCP 工具自动搜索景点、天气、路线，生成多日行程。代码与文档注释主要为中文。

## 目录结构
- `backend/` — FastAPI 后端（Python）。入口 `run.py` 或 `uvicorn app.api.main:app`。分层：
  - `app/api/routes/` — FastAPI 路由，统一挂载在 `/api` 前缀下（`trip.py`、`poi.py`、`map.py`）
  - `app/agents/` — 多智能体旅行规划（`trip_planner_agent.py`），通过 `MCPTool` 调用 amap MCP 服务
  - `app/services/` — 服务层（`amap_service.py`、`llm_service.py`、`unsplash_service.py`）
  - `app/models/schemas.py` — pydantic 数据模型
  - `app/config.py` — pydantic-settings + dotenv 配置
- `frontend/` — Vue3 + TypeScript + Vite + Ant Design Vue。`@` 别名指向 `src`。组件按 `views/`、`components/`、`services/`、`types/` 组织。

## 常用命令
后端（`cd backend`，虚拟环境已存在于 `backend/venv`，Python 3.13）：
- 安装：`pip install -r requirements.txt`
- 启动：`python run.py` 或 `uvicorn app.api.main:app --reload --port 8000`
- API 文档：`http://localhost:8000/docs`

前端（`cd frontend`）：
- 安装：`npm install`
- 开发：`npm run dev`（Vite，端口 5173，`/api` 代理到 8000）
- 构建 + 类型检查：`npm run build`（`vue-tsc && vite build`）

没有 lint 或单测脚本，也没有测试框架。

## 关键约定与易错点
- 配置全部经 `backend/app/config.py` 读取环境变量（`backend/.env`）。**`.env` 含真实密钥（DeepSeek、高德、Unsplash），已被 `.gitignore` 排除，严禁提交或打印明文。** `frontend/.env.example` 里疑似放了真实高德 Key，同样不要打印或外传。
- 环境变量名：后端 `AMAP_API_KEY`（Web 服务）、`UNSPLASH_ACCESS_KEY`；LLM 走 HelloAgents 约定 `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL_ID`（`OPENAI_*` 仅兜底，校验时以 `LLM_API_KEY` 优先）。前端 `VITE_API_BASE_URL`、`VITE_AMAP_WEB_JS_KEY`（高德 JS API Key，用在 `Result.vue` 的地图上，与后端那个高德 Key 是两个不同的 Key）。
- 前端 axios 直连后端（`src/services/api.ts`，`VITE_API_BASE_URL` 默认 `http://localhost:8000`），实际不走 Vite 的 `/api` 代理；跨域靠后端 CORS（已允许 5173/3000）。axios 超时 2 分钟（行程生成本来就慢，别调小）。路由在 `main.ts` 内联定义（`/` Home、`/result` Result），没有独立 router 目录。
- Agent 的工具调用有严格格式（agent 提示词中定义），例如 `[TOOL_CALL:amap_maps_text_search:keywords=...,city=...]`，改动 `agents/trip_planner_agent.py` 时不要破坏该格式，否则工具调用会失败。
- 后端依赖 `hello-agents[protocols]`，版本被固定为 `>=0.2.4,<=0.2.9`（见 `requirements.txt`），升级需谨慎。
- 后端大量使用 `print()` 做运行日志（不是 loguru，尽管它已列入依赖）。保持一致的风格即可。
- `helloagents_env`：`config.py` 会额外尝试加载工作区上一级的 `../HelloAgents/.env`（不覆盖已有变量）。
- 项目已经是 git 仓库，默认远程为 `origin`。操作前仍需用 `git status --short --branch` 和 `git remote -v` 确认实际分支、工作区和远程状态。

## Git 提交与推送规则
- 每个功能增加或 Bug 修复使用一个独立提交；相关代码、测试和 `docs/learning-log.md` 中对应的问题记录放在同一个提交中。
- 开始修改前先检查工作区。不得覆盖、回滚或夹带其他任务及用户已有的修改；暂时无法归属的文件保持未暂存状态。
- 提交前必须查看 `git diff` 和 `git diff --cached`，并运行与本次修改直接相关的验证命令。验证失败时不得将该提交描述为已完成。
- 提交前检查暂存文件，不得提交 `.env`、API Key、Token、凭据、真实用户数据、完整敏感模型响应或本地生成物。
- Commit message 使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`test:`、`refactor:` 或 `chore:`。标题简洁描述单一改动；正文记录 `原因`、`修改`、`验证`，方便学习和回溯。
- 完成功能或修复后，先向用户说明为什么修改、如何实现、需要注意的地方、文件差异和验证结果。只有收到用户明确的提交指令后才能执行 `git commit`。
- 只有收到用户明确的推送指令后才能执行 `git push`。默认推送当前任务分支；不得默认直接推送 `main`，首次推送分支时使用 `git push -u origin <branch>`。
- 禁止使用 `git push --force`、`git push --force-with-lease`、重写已共享提交或删除远程分支，除非用户明确要求并已了解影响。
- 不为未完成的功能创建普通完成提交；确需 WIP 提交时，必须由用户明确要求，并在提交标题中标注 `WIP`。

## 文档
改后端路由/Agent 前先读 `backend/app/config.py` 和 `README.md`（含架构图与 API 端点说明）。改前端前确认 `vite.config.ts` 的代理与别名。
