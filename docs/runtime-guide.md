# 运行手册

按需查阅的配置、部署、数据库维护和调试操作。首次安装及常用验证见 [README](../README.md)，业务规则见 [CONTEXT](../CONTEXT.md)，实施状态和验收证据见 [CHG-003](changes/CHG-003-planning-performance-observability.md)。本手册不维护进度或逐函数实现说明。

## 模型与地图配置

正式 v4 使用 Chat Completions 的 `tools` / `tool_calls`，供应商须支持原生工具调用。`MASTER_LLM_MODEL_ID` 与 `SUBAGENT_LLM_MODEL_ID` 分别指定主角色和三个采集子角色，共用 `LLM_BASE_URL`、`LLM_API_KEY`，空值才使用对应 `OPENAI_*` 配置兜底。角色模型ID不继承旧入口的 `LLM_MODEL_ID`。备用地址需手动切换，当前没有自动切换。

模型调用参数统一在`backend/app/config.py`声明，由环境变量/`backend/.env`覆盖，模型工厂读取；完整字段定义以配置代码为准。`LLM_TEMPERATURE`默认`0.01`，同时用于主/子角色及旧模型工厂。主/子角色参数如下：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `MASTER_LLM_REASONING_EFFORT` | 不传 | 服务端默认推理强度 |
| `SUBAGENT_LLM_REASONING_EFFORT` | `low` | 三个采集子角色共用 |
| `MASTER_LLM_MAX_OUTPUT_TOKENS` | `1000000` | 主角色输出上限，配置时须为正整数 |
| `SUBAGENT_LLM_MAX_OUTPUT_TOKENS` | `1000000` | 子角色输出上限，配置时须为正整数 |
| `LLM_OUTPUT_TOKEN_PARAMETER` | `max_tokens` | 可改为`max_completion_tokens`，按实际网关文档选择 |

输出上限必须是正整数，不接受字符串 `max`。请求额度不代表网关保证的实际输出长度；供应商兼容性需分别验证。旧模型入口不使用主/子角色输出上限。修改配置后重启后端。

`PLANNER_ENFORCE_TIMEOUTS` 默认 `false`，关闭新规划主动超时，明确的供应商错误、修复轮数和工具次数限制仍生效。设为 `true` 后恢复对应技术期限。相邻交通 worker 仍受 `PLANNER_EDIT_ROUTE_DEADLINE_SECONDS`（默认120秒）限制；数据库锁等待、服务退出等待和前端短请求期限另行管理。

高德 MCP 由 `backend/scripts/amap_stdio.py` 启动，独立 uvx 环境固定 `amap-mcp-server==0.1.11` 和 `pydantic==2.13.5`，以避开 MCP 1.8.1 与 Pydantic 2.14 的兼容问题；后端 requirements 不控制该子进程。`uvx` 需在 PATH 或 `~/.local/bin/uvx`，首次运行需能访问包源及缓存，部署时可设置 `UV_CACHE_DIR`。工具模块的调试 print 会被过滤，启动异常和 MCP 日志仍保留。

## PostgreSQL配置与旧数据迁移

三个仓库共用`DATABASE_URL`，分别使用前缀加`_observation`、`_preview`、`_destination`的schema。账号需要在已有库内创建这些schema及表/函数/触发器的权限，不需要超级用户、建库或建角色权限。本机与生产必须使用不同前缀；仍只运行单个后端进程。SQLite路径配置仅保留给显式离线测试和旧数据备份。

本机使用服务器`personal_web_dev`开发库及开发账号，schema前缀为`trip_local`；先建立SSH隧道，再启动后端，私有连接配置使用本机端口15432。隧道停止后数据库不可用，不自动改用本地旧库。生产使用`personal_web_prod`生产库及生产账号，schema前缀为`trip_prod`，连接服务器本机5432，不依赖这条隧道。两者共用PostgreSQL实例，但数据库和账号分开；生产库内其他项目的数据继续通过schema区分。数据库迁移的执行记录见[迁移记录](changes/CHG-003-planning-performance-observability.md#postgresql-migration-20261026)。

```bash
ssh -N -L 127.0.0.1:15432:127.0.0.1:5432 centos-cloud
```

迁移前停止所有写入并退出旧后端，用SQLite backup API制作三个一致性备份，保留代码、私有配置及回滚副本。以下命令从项目根目录运行，连接串和schema前缀预先配置在私有环境中；不在参数里传密码。

```bash
backend/venv/bin/python backend/scripts/migrate_to_postgres.py --tasks /backup/tasks.sqlite3 --previews /backup/previews.sqlite3 --candidates /backup/destination_candidates.sqlite3
# 只读源结构检查通过、目标schema为空后，显式执行：
backend/venv/bin/python backend/scripts/migrate_to_postgres.py --tasks /backup/tasks.sqlite3 --previews /backup/previews.sqlite3 --candidates /backup/destination_candidates.sqlite3 --apply --report /private/migration.json
backend/venv/bin/python backend/scripts/migrate_to_postgres.py --tasks /backup/tasks.sqlite3 --previews /backup/previews.sqlite3 --candidates /backup/destination_candidates.sqlite3 --verify-only --report /private/verification.json
```

迁移拒绝非空目标，逐表核对数量和内容哈希，保留凭据哈希、版本、历史字段及序列高水位。复制失败回滚业务数据，可能保留预先准备的空结构；源备份不删除。核对通过后才切换正式配置。若新库已接收用户写入，不得直接回退旧SQLite而丢弃新数据，应先停写并保全PostgreSQL。当前实际切换与验证状态见[唯一Change](changes/CHG-003-planning-performance-observability.md#postgresql-migration-20261026)。

真实PostgreSQL回归通过私有`TRIP_TEST_DATABASE_URL`连接开发库，测试创建并清理随机schema；无配置时跳过不能算PG验收。专项模块为`test_postgres_tasks`、`test_postgres_preview`、`test_postgres_candidates`、`test_postgres_migration`、`test_postgres_observation_api`。专项覆盖范围及执行证据以 Change 为准。

## 生产部署

前端生产构建部署到同域 `/trip/`，本地 `npm run dev` 仍访问 `/`。Vite 根据构建命令设置资源路径，Vue Router 使用同一基础路径；`frontend/.env.production` 设置 `VITE_API_BASE_URL=/trip`，实际接口请求为 `/trip/api/...`。前端环境变量在构建时写入产物，修改后必须重新构建；构建进程的同名环境变量及 `.env.production.local` 可以覆盖仓库配置，发布前应确认未携带旧地址。`VITE_*` 是公开配置，不得放入后端密钥。

服务器拉取源码后，在 `frontend` 执行 `npm ci` 和 `npm run build`，由 Nginx 提供 `dist` 内容。Nginx 需将 `/trip` 重定向到 `/trip/`，将 `/trip/api/...` 去掉 `/trip` 后转发至后端 `/api/...`，并为 `/trip/preview` 等前端路由回退到旅行助手的 `index.html`；若使用健康检查客户端，还需将 `/trip/health` 转发至后端 `/health`。后端环境、常驻服务和数据库目录须在服务器单独配置。本地构建不能代替服务器部署与在线验收；当前适配验证状态见[CHG-003](changes/CHG-003-planning-performance-observability.md#trip-subpath-deployment)。

生产继续拦截旧无鉴权 `/trip/api/trip/tasks` 及子路径和 `recover-local-access`，保留带任务 Bearer 的 v4 观测路径。前端主机名判断只选择展示模式，后端仍须逐任务鉴权；HTTP 不加密 Bearer，HTTPS 需单独配置。

## 任务观测与诊断

本机回环地址直接访问 `/observability` 可查看全部任务工作台，支持任务筛选、执行轨迹、Token、工具和异常分析。选中调用后读取脱敏详情；未知用量、耗时和截断均保留提示。任务总耗时不累加父子 span，指标不代表行程质量。公网用法见 [README](../README.md#本地任务观测)。

本机浏览器缺少旧任务凭据时，观测页可为已完成且未更新的预览轮换凭据；旧凭据随即失效。恢复仅允许本机请求，不重新规划，公网不提供恢复入口。

观测保留设置由 `backend/app/config.py` 读取：`OBSERVATION_RETENTION_DAYS` 默认7天，`OBSERVATION_MAX_BYTES` 默认200 MiB（逻辑内容治理目标），`OBSERVATION_CONTENT_LIMIT` 默认单步骤输入、输出各64 KiB。启动及接收任务前清理已结束的过期或超容量观测，活动任务保留；**观测清理不清理 v4 预览和地点候选仓库**。记录丢失不倒推根因，写入失败不改变业务判定；结果保存失败不能显示为成功。

排查已有任务时，先从项目根目录只读导出诊断，不重新提交规划：

```bash
backend/venv/bin/python backend/scripts/diagnose_task.py <task_id> --postgres
```

`--format json` 输出机器可读报告。`--db /path/tasks.sqlite3` 仅用于旧备份；未指定存储选项时也是旧默认备份路径，现行库必须显式传 `--postgres`。报告关联日期、轮次、版本和已记录错误，不读取完整模型回答、不修改任务；旧字段缺失保持未知。观测写入失败可检查控制台 `[observation]` 分类，重启不能恢复未落盘内容。

## 独立规划调试

以下命令在 `backend` 目录运行。固定资料均为合成数据；离线结果不代表真实地点核实或推荐质量通过。真实模式会调用配置的模型或高德，应显式选择案例并使用公开或合成需求。

### 仅规划模式

```bash
PYTHONPATH=. venv/bin/python scripts/run_planning_core.py --mode fake --input-file tests/fixtures/planning_core/normal.json
PYTHONPATH=. venv/bin/python scripts/run_planning_core.py --mode fake --cases normal no_breakfast_evidence insufficient_candidates specified_hotel unknown_cost_weather explicit_repeat rest_preference
```

`fake` 回放固定提案，不读取密钥或调用模型；改为 `--mode live` 才调用真实主模型。`--fixture-dir <目录>` 可替换资料目录。入口输出草稿、校验报告、独立评估与运行记录，不采集资料、补查、查询交通或保存正式预览。`complete/partial` 仅指必需地点覆盖；人工质量项默认 unknown，人工评价须绑定结果指纹。未知 usage 为 null，不填零。

### 真实采集与按日修订

```bash
PYTHONPATH=. venv/bin/python scripts/run_collected_planning.py --live --request-file /tmp/public-trip-request.json > /tmp/collected-planning.json
```

该入口执行需求解释、真实资料采集、初版与按日修订，输出快照、初版/最终结果、评估及分阶段耗时，不查询相邻交通或保存正式预览。技术失败保留已完成阶段，缺项耗尽可交付 partial。默认接入配置的 PostgreSQL 观测仓库；`--observation-db /tmp/research.sqlite3` 可隔离研究记录，挂接已有库不执行进程恢复。观测写入不完整不改业务结果；生成耗时包含内部提交校验，不能与子 span 重复相加。

其他专项脚本位于 [backend/scripts](../backend/scripts)，参数以各脚本的 `--help` 为准；历史样本日期可能已超出天气预报范围。对应阶段证据见 [分层计划](changes/CHG-003-planning-performance-observability.md#modular-planning-20261021)。

## 结果页交互原型

开发模式访问 `/prototype/result`；生产不注册该路由。原型使用高德真实底图和查询，但候选、费用、行程安排及保存恢复仍含固定示例与专用本机缓存，不连接正式规划 API 或数据库。页面底部可模拟失败和重置示例，PNG 导出会实际下载。地图需前端高德 JS Key 与安全码；原型效果不能作为正式功能验收，历史证据见 [CHG-003](changes/CHG-003-planning-performance-observability.md#第一步实施与验收2026-10-10授权)。
