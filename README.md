# HelloAgents 智能旅行助手

基于 Vue 3、FastAPI 和 HelloAgents 高德 MCP 工具的旅行规划助手。主智能体协调景点、餐饮和住宿子智能体，生成可按日编辑、查看地图及导出 PNG 的行程。

## 快速开始

本机日常运行使用 `production/helloagents-trip-planner` 主目录；独立 worktree 的修改需整合并验证后才会在此生效。以下命令适用于 macOS / Linux。

1. 准备 Python（本机使用3.13）、Node.js（须满足 `frontend/package.json` 中 Vite 的要求）、uv/uvx、可访问的 PostgreSQL，以及模型和高德 API 配置。
2. 在项目根目录安装依赖；已有 `backend/venv` 时跳过创建步骤：

   ```bash
   python3 -m venv backend/venv
   backend/venv/bin/python -m pip install -r backend/requirements.txt
   npm --prefix frontend ci
   ```

3. 首次配置时复制环境示例，再填写下表中的实际值；已有 `.env` 时直接编辑，不要覆盖：

   ```bash
   cp backend/.env.example backend/.env
   cp frontend/.env.example frontend/.env
   ```

   | 文件 | 必要配置 |
   | --- | --- |
   | `backend/.env` | `LLM_API_KEY`、`LLM_BASE_URL`、`MASTER_LLM_MODEL_ID`、`SUBAGENT_LLM_MODEL_ID`；模型须支持原生工具调用 |
   | `backend/.env` | `AMAP_API_KEY`（高德 Web 服务 Key） |
   | `backend/.env` | `DATABASE_URL`、`DATABASE_SCHEMA_PREFIX`；正式存储为 PostgreSQL，缺配置不回退 SQLite |
   | `frontend/.env` | `VITE_AMAP_WEB_JS_KEY`、`VITE_AMAP_WEB_JS_SECURITY_CODE`（浏览器地图配置） |

   `.env` 不提交；`VITE_*` 会进入浏览器，不能填写后端密钥。完整字段和默认值以 [config.py](backend/app/config.py) 为准，模型参数与数据库权限见[运行手册](docs/runtime-guide.md)。环境中同名变量优先，修改后端配置后需重启服务。

4. 数据库可连接后，在项目根目录启动：

   ```bash
   python3 dev.py
   ```

访问 [本地页面](http://127.0.0.1:5173) 和 [API 文档](http://127.0.0.1:8000/docs)。`Ctrl+C` 同时停止两端；前端热更新，后端代码改动后需重启脚本。端口被占用时脚本退出并停止另一服务。

前端 axios 默认直连 `http://localhost:8000`，可用 `VITE_API_BASE_URL` 覆盖；当前不依赖 Vite 的 `/api` 代理。跨域由后端 CORS 配置允许。

## 日常使用

1. 打开 `/` 或 `/plan`，填写城市、到达和离开日期时间、备注等需求。预算为**人均预算（元/全程）**；已订酒店需从同城搜索结果中选择。到离地点选填。
2. 提交后进入 `/preview` 查看真实阶段与结果。地点先交付，相邻交通独立查询；交通等待或失败不妨碍查看、编辑和导出。
3. 按日拖动排序、删除活动、搜索添加景点或餐馆，并选择已查询的交通方式。修改城市、日期或其他整体需求需重新规划。
4. “导出全部日期”按当前快照生成 PNG，长日分页；下载后尝试保存最终结果，保存失败会单独提示。“还原上次保存”恢复最近一次成功保存的快照。
5. 顶部“规划记录”进入 `/history`，切换本浏览器保存了凭据的规划；单条不可读时可刷新。换浏览器、无痕窗口、网站来源变化或清除网站数据后不会自动找回。

预览按时段展示地点顺序，不提供精确活动时刻、停留时长或返程可行性保证。交通只作参考；天气、消费等缺失信息保留未知。已知消费小计不等于完整旅费，酒店地点核实不保证房态或旅行日期报价。

## 本地任务观测

从行程的“规划过程”查看当前任务的执行轨迹、模型与工具调用、Token 和异常。浏览器按任务保存访问凭据，URL 不含凭据；仅复制链接不能取得访问权。公网不提供全部任务列表或凭据恢复。

本机回环地址可直接访问 `/observability` 使用管理工作台；观测保留期限、旧凭据恢复和只读诊断命令见[运行手册](docs/runtime-guide.md#任务观测与诊断)。

当前仅支持单后端进程，不使用多个 worker 共享任务运行状态。关闭浏览器不会取消规划；后端正常退出会等待活动任务，强制退出后未完成任务在下次启动标记中断，不自动重跑。

## 验证

后端标准库 unittest（在 `backend` 目录）：

```bash
PYTHONPATH=.:tests venv/bin/python -m unittest discover -s tests -v
```

前端类型检查与生产构建（在 `frontend` 目录）：

```bash
npm run build
```

前端专项回归位于 `frontend/tests/`，按改动选择文件，例如 `node --test tests/v4PreviewAccessStorage.test.mjs`；当前没有 npm test/lint 脚本。真实 PostgreSQL 专项使用私有 `TRIP_TEST_DATABASE_URL`，无配置而跳过不能算数据库验收，模块入口见[运行手册](docs/runtime-guide.md#postgresql配置与旧数据迁移)。

模拟测试、构建、真实服务核验和人工接受分别记录。完整离线回归需封锁外部模型和地图连接；测试通过不代表真实行程质量已接受。当前范围及证据见 [CHG-003](docs/changes/CHG-003-planning-performance-observability.md#当前状态)。

## Deployment

生产前端部署在 `/trip/`，API 基址为同域 `/trip`；本地开发仍使用 `/`。前端配置在构建时写入，修改后必须重新构建。Nginx 转发、数据库隔离和迁移步骤见[运行手册](docs/runtime-guide.md#生产部署)。

## 文档入口与记录边界

**README 只维护当前使用入口。** 保留项目用途、首次启动、必要配置、常用操作、验证入口和影响使用的限制。功能变化时改写对应段落，不在文末追加实施流水账。

| 内容 | 记录位置；README 的处理方式 |
| --- | --- |
| 按需使用的配置、部署、迁移与调试命令 | [运行手册](docs/runtime-guide.md)；README 只留常用步骤和链接 |
| 稳定业务术语与规则 | [CONTEXT](CONTEXT.md)；README 只说明用户需要知道的限制 |
| 需求、范围、待办、实施及验收状态 | [当前 Change](docs/changes/CHG-003-planning-performance-observability.md)；不复制进度表 |
| 问题、原因、取舍与有日期的验证结论 | [学习日志](docs/learning-log.md)、[独立问题记录](docs/problems/README.md)；不追加单次测试数量、耗时、提交或部署流水 |
| 学习目标 | [学习计划](docs/learning-plan.md)；不复制阶段任务 |
| 架构与源码阅读入口 | [项目实现地图](docs/project-overview.html)、[设计参考](docs/planning-design.html)、[工程指标设计](docs/engineering-metrics-design.md)；不复制函数说明、完整字段或接口清单 |
| 协作与文档维护规则 | [AGENTS.md](AGENTS.md)；每项事实只维护一处，其他入口链接 |

API 参数以运行中的 `/docs` 和代码为准；历史方案、失败实验及旧接口实现留在对应 Change，不写成现行用法。密钥、真实用户数据、完整模型响应和本地生成物不写入项目文档。

<a id="sole-planning"></a>
独立仅规划、真实采集 CLI 和[结果页交互原型](docs/runtime-guide.md#结果页交互原型)的入口统一见[运行手册](docs/runtime-guide.md#独立规划调试)。它们用于专项调试，验收范围见对应 Change。

项目实现地图通过根目录静态服务访问：`python3 -m http.server 8765 --bind 127.0.0.1`，然后打开 `http://127.0.0.1:8765/docs/project-overview.html`。设计参考可直接打开；图示的核对范围以页内说明为准。

## 协议与致谢

CC BY-NC-SA 4.0。感谢 [HelloAgents 教程](https://github.com/datawhalechina/Hello-Agents)、[HelloAgents 框架](https://github.com/jjyaoao/HelloAgents)、[高德地图](https://lbs.amap.com/) 和 [amap-mcp-server](https://github.com/sugarforever/amap-mcp-server)。
