# 项目修复执行路线图

## 目的

本文档用于跟踪代码审查发现的可靠性、安全性、数据安全和运维界面问题。修复按独立 PR 推进，避免同时改动请求生命周期、客户端状态和数据发布流程。

## 状态说明

- `[ ]` 未开始
- `[-]` 进行中
- `[x]` 已完成
- `[!]` 阻塞，需要决策或外部条件

更新任务状态时，同时填写对应阶段的“执行记录”和“验收证据”。任务只有在验收条件得到可重复证据后才能标记为完成。

## 总览

| 阶段 | 内容 | 状态 | 预计工作量 | 依赖 | 建议 PR |
|---|---|---|---:|---|---|
| 0 | 生产事实确认与 CN 范围决策 | `[x]` 已完成 | 0.5 天 | 无 | PR 1 (#5 merged) |
| 1 | 测试基线与 CI | `[x]` 已完成 | 1 天 | 阶段 0 | PR 2 (#6 merged) |
| 2 | 凭据、日志和内部 RPC 安全 | `[x]` 已完成 | 1-2 天 | 阶段 1 | PR 3 (#7 merged) |
| 3 | Dashboard 安全与交互 | `[-]` 进行中（待桌面/移动端手工验收） | 0.5-1 天 | 阶段 1 | PR 4 |
| 4 | 定时任务互斥与 Git 数据安全 | `[x]` 已完成 | 1-2 天 | 阶段 1 | [PR #9](https://github.com/Sekai-World/sekai-client/pull/9) merged |
| 5 | 区域 bootstrap 与客户端状态机 | `[-]` 已实现生命周期/readiness 代码切片，生产验收待完成 | 2-3 天 | 阶段 1 | 未提交（`<commit-id>`、`<commit-id>`） |
| 6 | Deadline、重试与队列生命周期 | `[x]` 已完成 | 2-3 天 | 阶段 5 | PRs #21-#24 |
| 7 | Event tracker outbox 与 API 响应校验 | `[ ]` | 2-4 天 | 阶段 1；建议在阶段 6 后 | PR 8-9 |

预计总工作量：10-16 个工程日。阶段 0-4 优先止血，阶段 5-7 处理结构性风险。

## 关键决策

### D-001：CN 是否属于正式支持区域

- 状态：`[x]` 已完成（决策：方案 B 的变体——CN 不属正式服务区域，但保留独立简化版 `checkUpdate-cn` 进程）
- 原因：`Config.REGIONS` 和 PM2 声明支持 CN，但 `initial_api_headers` 缺少 CN 配置，`app_id_regions` 也缺 CN，CN 缺少完整 headers/登录/只读接口支持。
- 方案 A：确认支持，补全并验证 CN headers、版本检查、登录和只读接口。
- 方案 B：暂不支持，从运行配置、公共 API、Dashboard 和文档中移除 CN。
- 决策：**CN 未正式部署。从正式服务区域声明（`Config.REGIONS`、`api_public_server.client_map`、PM2 `regions`）移除 CN，但保留简化版 `checkUpdate-cn` 进程（`CHECK_UPDATE_SIMPLE_MODE=true`），因其依赖独立配置（`nuverse_master_data_base_url`、简单版本 URL）不依赖 `initial_api_headers`/`app_id_regions`。**
- 决策日期：2026-07-16
- 决策人：实施阶段 0（remediation）

## 阶段 0：确认范围和生产事实

### 目标

确认静态审查结论是否影响实际部署，并明确后续修复边界。

### 任务

- [x] 确认 CN 不属于正式支持区域，完成 D-001（保留简化版 `checkUpdate-cn` 进程）。
- [x] 自检代码库区域声明：从 `Config.REGIONS` 与 `api_public_server.client_map` 移除 CN；生产 PM2 使用 `<protected-ops-dir>` 下的一进程一个 YAML，正式服务不含 CN，保留独立 `checkUpdate-cn` 与 `CN_PORT`/port-map。
- [x] 检查生产环境实际启动的区域和 PM2 进程。
  - 状态：`[x]` 2026-08-13 在 <production-host> 确认 预期业务进程在线：JP/EN/TW/KR 各有 shared/check/event，另有 `sekai-api` 与 standalone `checkUpdate-cn`。
- [x] 确认 shared client 是否始终绑定 `127.0.0.1`。
  - 状态：`[x]` PM2 参数与 `ss -lntp` 均确认四个 shared client 分别绑定 `loopback-only-service-bindings`；未对外监听。
- [x] 确认每个 shared client 的 Gunicorn worker 数量。
  - 状态：`[x]` 2026-08-13 的进程树确认每个 shared client 为一个 Gunicorn master 加一个 worker；未配置额外 workers。
- [x] 确认 master/i18n 仓库是否可能存在未推送 commit。
  - 状态：`[x]` JP/EN/KR/TW/CN master 仓库与共享 i18n 均为干净的 `main...origin/main`，且 `log --branches --not --remotes` 无输出。
- [x] 记录当前 pytest、Ruff 结果作为基线（见“验收证据”）。
- [x] 增加启动期/配置层区域映射完整性校验（`Config.validate_region_config`，并在 `api_public_server.bootstrap` 启动失败）。

### 验收条件

- [x] 所有声明支持的区域（jp/en/tw/kr）均具有 headers、URL、端口等完整配置。
- [x] 缺失区域配置时，进程在启动阶段给出明确错误（`Config.validate_region_config` + `api_public_server.bootstrap` 抛 `RuntimeError`）。
- [x] 生产部署约束已记录，不再依赖未文档化假设。
  - 状态：`[x]` 拓扑、loopback、单 worker 与全部数据仓库状态均已通过 2026-08-13 只读生产审计确认。

### 验收证据

- 测试（执行于 2026-07-16，仓库内 `pytest` 子集）：
  - `pytest tests/test_config.py -q`：新增 `TestRegionConfigValidation` 全部通过（含 CN 已从 `REGIONS` 排除、`validate_region_config` 缺映射报错）。
  - `ruff check .`：通过（仅阶段 0 范围内改动）。
- 代码层改动：
  - `config.py`：`REGIONS` 移除 `cn`；新增 `validate_region_config` 并接入 `Config.validate`。
  - `api_public_server.py`：`client_map` 移除 `cn`；`bootstrap` 在区域映射不完整时启动失败。
  - 生产 PM2：已确认 <production-host> 使用 `<protected-ops-dir>` 下 14 个独立 YAML；4 个正式区域各有 shared/check/event，另有 `sekai-api` 与 standalone `checkUpdate-cn`（simple mode）。仓库 `deployment/pm2/examples/` 提供对应无 secret 模板。
  - 2026-08-13 生产审计：PM2 与进程树确认四个正式区域各一套 shared/check/event，shared client 均为 loopback + 单 worker；JP/EN/KR/TW/CN master 与共享 i18n 仓库均干净且无未推送提交。
  - EN 观察项：`checkUpdate-en` 与 `eventTracker-en` 累计重启分别为 aggregate historical restart data，但均连续在线约两天、`unstable_restarts=0`。最后退出码为 130，证据不显示当前 crash loop；历史累计次数的具体原因未推断，作为非阻塞观察项保留。

### 执行记录

- 2026-07-16：完成 D-001 决策与代码层阶段 0 改动。CN 从正式区域声明移除，保留简化版 `checkUpdate-cn`。新增启动期区域映射完整性校验并接入 `Config.validate` 与 `api_public_server.bootstrap`。新增聚焦测试 `tests/test_config.py::TestRegionConfigValidation`。未做 CI/安全/状态机（属阶段 1-7）。生产运行事实类检查项标记为待运维确认。
- 2026-08-13：完成 <production-host> 只读生产审计。确认 预期业务进程拓扑、四个 shared client 的 loopback 监听和单 Gunicorn worker；所有 master 与共享 i18n 仓库均干净且无未推送提交。EN 两进程虽有较高累计重启数，但当前稳定且无 unstable restart，阶段 0 关闭。

## 阶段 1：测试基线与 CI

### 目标

在修改关键行为前建立可重复的验证路径。

### 任务

- [x] 增加所有支持区域的配置完整性测试。（`tests/test_config.py::TestRegionConfigValidation`，含 fail-fast 与幂等安全边界）
- [x] 增加 `api_public_server` bootstrap 不完整配置快速失败测试（`test_config.py::test_bootstrap_rejects_incomplete_region_config`）。
  - `[!]` deferred：bootstrap 部分区域失败仍继续标记 `bootstrapped=True`（`api_public_server.bootstrap` 用 try/except 跳过失败区域）的真实回滚/部分失败恢复测试，需等阶段 5 状态机重构后才能稳定成立，未固化为期望。
- [x] 增加 shared client 已登录缓存不重复 login 与失败回滚测试（`tests/test_shared_client.py`：`test_already_logged_in_returns_cache_without_relogin`、`test_failed_forced_login_restores_active_session`）。
- [x] 增加队列满快速拒绝/异常契约测试（`tests/test_shared_client.py::test_enqueue_job_rejects_with_error_when_queue_full`）。
- [x] 增加 check_update push 失败返回契约测试（`tests/test_check_update.py::test_commit_master_diff_returns_false_on_push_failure`），并确认执行到远端 push 错误。**不得断言 `rmtree` 为正确行为**，push 失败删除仓库的数据丢失问题标为阶段 4 deferred。
  - `[!]` deferred：非幂等请求（POST）不得自动重试的测试——当前 `call_pjsk_api(retry_after_error=True)` 默认对所有错误重试，无按方法/幂等性区分；需阶段 6 deadline/重试策略重构后成立。
  - `[!]` deferred：update job 互斥测试——需阶段 4 进程级互斥锁/调度 `max_instances=1` 后才能成立。
  - `[!]` deferred：push 失败保留本地 commit 与数据测试——`shutil.rmtree` 删除仓库为已知错误行为，阶段 4 修复前无法"保留本地数据"，故只锁定失败返回契约，不固化 rmtree。
- [x] 增加最小 CI：Ruff、mypy、pytest（`.github/workflows/ci.yml`，Python 3.12 + astral-sh/setup-uv，权限 read-only，针对 `transform-python` 分支）。
- [x] 记录当前 Ruff/mypy 已知问题，不在本阶段混入全仓清理（仅机械修复 `tests/` 与 `service_dashboard.py` 的格式/未用 import/空白，零逻辑变化）。

### 最小验证命令

```bash
uv run --extra dev ruff format --check .
uv run --extra dev ruff check .
uv run --extra dev mypy api_client.py shared_client.py utils/ config.py
uv run --extra dev pytest tests/
```

### 验收条件

- [x] PR 自动执行 lint、类型检查和测试。
- [x] 关键失败路径具备回归测试（登录缓存、bootstrap fail-fast、队列满、push 失败返回）。
- [x] CI 失败能阻止合并。
  - 状态：`[x]` 2026-08-13 仓库公开后已激活目标为默认分支的 `protect default` ruleset；严格要求真实 check context `Lint, type-check and test` 通过，并要求通过 PR 合并、禁止删除和 non-fast-forward push。Bypass list 为组织管理员和仓库管理员，仅用于紧急恢复。PR #19 验证了规则会阻止不满足条件的合并，并在 required check 通过后允许正常合并。

### 验收证据

- CI 文件：`.github/workflows/ci.yml`（Python 3.12，`astral-sh/setup-uv@v5`，`actions/checkout@v4` + `actions/setup-python@v5`，`permissions: contents: read`，触发 `push`/`pull_request` 到 `transform-python`）。
- scoped mypy 选择：`api_client.py shared_client.py utils/ config.py`——覆盖请求生命周期、客户端状态、区域配置与后台任务队列；其余模块（含已知全仓 mypy 问题的 `event_tracker.py`、`check_update.py`、`api_public_server.py`、`service_dashboard.py`、`dashboard/`）不在阶段 1 范围，避免混入全仓清理。
- 已知全仓 mypy 问题（未在本阶段修复，仅记录）：运行全仓 `mypy .` 会暴露 `event_tracker.py`、`check_update.py`、`api_public_server.py`、`service_dashboard.py` 等模块的类型错误（`warn_return_any` / `warn_redundant_casts` / `warn_unused_ignores` 等）；scoped 命令当前 `Success: no issues found in 14 source files`，说明核心模块已通过，全仓问题留待对应阶段（3/4/5/7）随代码改动处理。
- 测试数量：本阶段 `pytest tests/ -q` 共 **76** 个用例（基线 71 + 新增 5：登录缓存、bootstrap fail-fast 安全边界 x2、队列满拒绝、push 失败返回契约）。
- 本地验证命令结果（2026-07-16）：
  - `uv run --extra dev ruff format --check .`：pass（29 files already formatted）
  - `uv run --extra dev ruff check .`：pass（All checks passed）
  - `uv run --extra dev mypy api_client.py shared_client.py utils/ config.py`：pass（no issues found in 14 source files）
  - `uv run --extra dev pytest tests/ -q`：76 passed
  - `git diff --check`：clean

### 执行记录

- 2026-07-16：新增 `.github/workflows/ci.yml`（阶段 4 之前脚手架，read-only 权限，针对 `transform-python`）。机械修复 `tests/` 与 `service_dashboard.py` 的格式/未用 import/空白以通过 lint 门禁（零逻辑变化）。扩展 `tests/test_shared_client.py` 覆盖已登录缓存不重复 login 与队列满快速拒绝；扩展 `tests/test_config.py` 覆盖 bootstrap 配置 fail-fast 与幂等安全边界；新增 `tests/test_check_update.py` 锁定 push 失败返回契约、确认执行到远端 push 错误，但不把 rmtree 固化为正确行为。下列原任务因依赖阶段 4/5/6 修复而标记为 deferred，未伪称完成：bootstrap 部分失败恢复、POST 非幂等自动重试、update job 互斥、push 失败保留本地数据。`pytest` 71 → 76 passed。`git diff --check` clean。
- 2026-07-16：阶段 1 PR [#6](https://github.com/Sekai-World/sekai-client/pull/6) 的 CI 检查通过后合并到 `transform-python`，合并提交 `<commit-id>`。代码与 CI 基线工作已完成；仓库分支保护是否把该检查配置为 required status check 仍需在 GitHub 仓库设置中确认，不以 PR 成功合并替代该运维事实。

## 阶段 2：凭据、日志和内部 RPC 安全

### 目标

阻断凭据经日志、URL、文件和高权限 RPC 泄露的路径。

### 任务

- [x] 实现集中式日志脱敏（`utils/redaction.py`：`SecretRedactingFilter` 递归脱敏 dict/list 与文本脱敏；`logging_config.configure_logging` 默认安装，`attach_redaction` 供 gunicorn 入口复用）。
- [x] 屏蔽 `authorization`/`cookie`/`set-cookie`/`x-session-token`/`credential`/`signature`/`accessToken`/`access_token`/`token`/`api_key`/`x-api-token`/`x-api-key`/`device_id`/`x-install-id` 等键与 `Bearer`/header-like/URL query（`[REDACTED]` 替换）。
- [x] `shared_client` 请求/错误日志面显式缩小：`run_job` 在把错误 `data` 序列化回内部调用方前先脱敏。
- [x] `check_update._post_strapi_ids`：URL 不再含 token；改用 `Authorization: Bearer` 与 `X-Strapi-Token` header，并调用 `raise_for_status` 后捕获 `requests.RequestException` 记录错误继续，避免辅助 Strapi 故障阻断主数据更新；移除 legacy query fallback。
- [x] `service_dashboard._scan_logs` 的 `recentErrors` 在返回前脱敏。
- [x] 凭据 YAML 原子写入（`shared_client._write_account_yaml_atomic`：同目录临时文件、`0600`、`flush`/`fsync`/`os.replace`/异常清理、`yaml.safe_dump`）。
- [x] 内部 RPC 鉴权（`Config.get_internal_rpc_token` / `ALLOW_INSECURE_INTERNAL_RPC` 动态读取；header `x-internal-rpc-token`；`compare_digest` 常量时间比较；默认 token 缺失 500，错误 token 401；仅 `ALLOW_INSECURE_INTERNAL_RPC=true` 且 loopback 才 bypass，非 loopback 一律 401；未认证请求不启动 scheduler）。
- [x] `utils.jsonrpc_client.JSONRPCClient.request` 自动动态读 token 并加 header；缺 token 本地 `RuntimeError`（fail-closed）；HTTP 响应在 `json` 解析前 `raise_for_status`；所有调用方经默认 client 自动兼容，无手工散落 token。
- [x] PM2 模板：正式 `shared/check/event` 与 public API 传递同一 `INTERNAL_RPC_TOKEN`；production 不传 `ALLOW_INSECURE_INTERNAL_RPC`；standalone `checkUpdate-cn` 不依赖 RPC。模板采用与生产一致的一进程一个 YAML，并按进程/区域最小化外部凭据。
- [x] `shared_client.account_info` RPC 仅返回 `userId` 与 `region`，兼容 `event_tracker`。
- [x] `request_and_decrypt` 在 `APIClient` 内严格校验：仅 GET、空 body、https、当前 region `nuverse_master_data_base_url` host/base path、`master-data-<digits>.info` 文件名；拒绝 path traversal（`..`）/非白名单 host/scheme。
- [x] 收敛通用 `call_pjsk_api`：新增 `fetch_master_split(split_path)` 仅允许 `client.master_split_paths` 中值（GET）；`check_update` 改调此 RPC；通用 `call_pjsk_api` 默认禁用，仅 `ENABLE_UNSAFE_PJSK_RPC=true` 时允许（加入 `Config` 动态布尔）。未做完整 capability model。

### 验收条件

- [x] 日志中不出现 credential、session token 或 access token。
- [x] URL 中不再包含认证 token。
- [x] 未认证请求不能调用内部 RPC（且非 loopback 一律拒绝）。
- [x] RPC 不能向任意 URL 发起请求（`request_and_decrypt` 白名单 + 通用 RPC 默认禁用）。
- [x] 合法内部调用保持可用（带 token 或 loopback dev bypass）。

### 验收证据

- 日志脱敏：`tests/test_redaction.py`（结构/文本/Bearer/header/URL query/日志 filter 单测，断言 secret 不出现在输出）。`configure_logging` 默认安装 filter；`shared_client.run_job` 对错误 `data` 脱敏；`service_dashboard._scan_logs` 对 `recentErrors` 脱敏。
- 内部 RPC 鉴权：`tests/test_internal_rpc_auth.py` 覆盖 server 端（缺 token→500、错 token→401、正确 token→通过、loopback dev bypass、非 loopback→401、未认证不启动 scheduler）与 client 端（自动加 token header、缺 token 本地 RuntimeError、HTTP 错误先 raise）。`tests/test_jsonrpc_client.py` 增加 autouse fixture 提供 dev token。
- Strapi：`tests/test_check_update.py::test_post_strapi_ids_uses_authorization_header_not_query` 与 `..._logs_and_continues_on_http_error`（header 化、无 query token、调用 `raise_for_status`，HTTP 错误不传播）。
- YAML 原子性：`tests/test_shared_client.py::test_write_account_yaml_atomic_mode_0600_and_cleanup_on_failure`（0600、替换、失败清理临时文件，不真实碰用户文件）。
- account_info 字段：`tests/test_shared_client.py::test_account_info_rpc_returns_only_userid_and_region`（仅 userId/region）。
- request_and_decrypt 白名单：`tests/test_api_client.py` 4 例（允许名单 URL、拒绝非 GET/非 https/错误 host/traversal/bad filename）。
- fetch_master_split / unsafe 开关：`tests/test_api_client.py` + `tests/test_shared_client.py`（`fetch_master_split` 允许名单调用、拒绝未允许路径；`call_pjsk_api` 默认禁用、开关开启可用）。
- 测试数量：阶段 2 实施期间 `pytest tests/ -q` 从阶段 1 基线 76 增至 **119** 个用例（最终 PR CI 通过）。
- 本地验证命令结果（2026-07-16）：
  - `uv run --extra dev ruff format --check .`：pass（32 files already formatted）
  - `uv run --extra dev ruff check .`：pass（All checks passed）
  - `uv run --extra dev mypy api_client.py shared_client.py utils/ config.py`：pass（no issues found in 15 source files）
  - `uv run --extra dev pytest tests/ -q`：119 passed（PR #7 最终提交）
  - `deployment/pm2/examples/*.yaml.example`：PyYAML 解析通过，并验证正式服务包含 `INTERNAL_RPC_TOKEN`、CN 不包含内部 token、无生产不安全开关。
  - `git diff --check`：clean

### 执行记录

- 2026-07-16：实现阶段 2 最小安全设计。新增 `utils/redaction.py`（递归+文本脱敏、`SecretRedactingFilter`、`attach_redaction`），`logging_config.configure_logging` 默认安装 filter。`Config` 增加动态内部 RPC 安全配置；client/server 默认 fail-closed，account_info/RPC capability/URL allowlist/YAML 写入与日志脱敏均收敛。`check_update` 的 Strapi token 改为 header。README 增加内部 token 部署说明。后续生产盘点确认 <production-host> 实际使用 `<protected-ops-dir>` 的独立 YAML，因此仓库改为 `deployment/pm2/examples/*.yaml.example`，而不是误导性的单一 ecosystem 文件。**明确延期项（未伪称完成）**：secret store、mTLS/Unix socket、完整 capability model；未改变阶段 4 Git 删除 / 阶段 5 状态机 / 阶段 6 重试。最终验证全绿（119 passed，mypy/ruff/YAML/git clean）。
- 2026-07-16：阶段 2 PR [#7](https://github.com/Sekai-World/sekai-client/pull/7) 在最终 CI 通过后 squash 合并到 `transform-python`，合并提交 `<commit-id>`。

## 阶段 3：Dashboard 安全与交互

### 目标

避免误操作、状态误导、HTML 注入和管理 token 不必要的长期暴露。

### 任务

- [x] 统一状态模型：Healthy、Degraded、Probe failed、Offline、Missing、Restarting。
- [x] 保证状态颜色与文案来自同一个归一化字段。
- [x] 单服务重启增加服务名和区域确认。
- [x] 区域重启显示全部受影响服务并使用更强确认（列出受影响服务，并要求输入区域名）。
- [x] 重启期间禁用相关操作，防止重复提交。
- [x] 区分重启失败与重启成功后刷新失败（`restartStatus`: `success` / `restart_failed` / `refresh_failed`）。
- [x] 使用 `createElement`、`textContent` 和属性赋值替代动态 `innerHTML`。
- [x] token 默认仅保存在当前会话（`sessionStorage`）。
- [x] 提供显式“记住此设备”（`localStorage`）和清除 token 操作。
- [ ] 验证桌面端和移动端操作流程。
  - 状态：`[!]` 待真实浏览器验证；代码已包含响应式布局、键盘 focus 与 reduced-motion 支持，但尚未记录桌面/移动端完整操作证据。

### 验收条件

- [x] 不会出现红色 `online` 等颜色与文字矛盾状态。
- [x] 重启不能通过一次误点直接触发。
- [x] 服务端返回的动态字段不能注入 HTML。
- [x] 用户能够区分进行中、成功、重启失败和刷新失败。
- [ ] 桌面端和移动端均可完成登录、查看状态和重启流程。
  - 状态：`[!]` 待在可连接 Dashboard API/PM2 的环境中完成手工验收。

### 验收证据

- DOM/XSS：`dashboard/index.html` 不再使用 `innerHTML`、`outerHTML` 或 `insertAdjacentHTML`；区域、服务名、类型、状态与错误日志等服务端动态值均通过 `textContent`/`createTextNode` 写入。JavaScript 语法检查与禁用 API 搜索通过。
- 状态模型：`tests/test_service_dashboard.py` 覆盖 missing/offline/restarting/degraded/probe_failed/healthy 的派生与优先级，并断言兼容字段 `ok` 严格由 `state == "healthy"` 派生。
- 重启结果：`tests/test_service_dashboard.py` 与新增 `tests/test_dashboard_api.py` 覆盖 success/restart_failed/refresh_failed、区域聚合、旧 `status` 兼容映射及未认证 401。前端显式读取 HTTP 200 响应中的 `refresh_failed`，不会误报成功。
- 聚焦验证（2026-07-17）：`uv run --extra dev pytest -q tests/test_service_dashboard.py tests/test_dashboard_api.py`：24 passed；Dashboard JavaScript syntax check：pass；`git diff --check`：clean。
- 全套回归（阶段 3 实施时）：`uv run --extra dev pytest tests/`：139 passed。
- 桌面和移动端手工验证记录：待填写；未真实触发线上 PM2 重启。

### 执行记录

- 2026-07-17：在分支 `security/phase-3-dashboard-safety` 实现阶段 3。后端新增单一归一化 `state`，兼容 `ok` 由其派生；重启 API 新增结构化 `restartStatus` 并保留旧 `status` 字段。Dashboard 改为安全 DOM 构建，增加单服务确认、区域强确认、操作期间禁用、成功/重启失败/刷新失败反馈，以及 session-only/记住设备/清除 token 语义；完善响应式布局与可访问性。自动化与静态验证通过；真实桌面/移动端流程和 PM2 重启仍待部署环境手工验收，因此阶段 3 保持部分完成，不提前标记全部验收完成。

## 阶段 4：定时任务互斥与 Git 数据安全

### 目标

防止 04:00 定时任务重叠、部分数据发布和 push 失败导致的数据丢失。

阶段 4 已完成 Strapi ID 持久化 outbox。`event_tracker` 的排名 outbox 不属于本阶段，仍是阶段 7 的未实现工作。

### 任务

- [x] 合并 update cycle，并为完整流程增加进程级与仓库级互斥锁。
- [x] 锁覆盖 fetch、获取版本、生成、校验、commit 和 push。
- [x] Scheduler 设置 `max_instances=1`、`coalesce=True` 和 `misfire_grace_time=300`。
- [x] 移除 commit/push 失败后的 `shutil.rmtree`。
- [x] 区分 fetch、fast-forward、commit、push、阻塞和待重试状态。
- [x] push 失败时保留本地 commit，并在后续周期优先恢复 pending push。
- [x] 文件先写入同文件系统 staging 路径，校验成功后使用 `os.replace` 原子替换。
- [x] 将 `versions.json` 放到跨 master/i18n 发布流程的最后一步。
- [x] 评估临时 Git worktree；当前不采用，使用 staging + 文件级原子替换满足现有消费者边界。

### 验收条件

- [x] 每天 04:00 只执行一个更新周期。
- [x] 两次并发触发时，第二次被排队或明确跳过。
- [x] push 失败不会删除仓库、working tree 或本地 commit。
- [x] 生成中途失败不会暴露新旧版本混合的数据集。

### 验收证据

- `tests/test_update_cycle_safety.py`：调度唯一性、04:00 daily/ordinary 语义、staging/校验失败、全局 `versions.json` 最后发布、空 manifest、candidate/global 状态与入口边界。
- `tests/test_git_lock_integration.py`、`tests/test_update_cycle_lock_integration.py`：真实 `multiprocessing/spawn` 锁竞争、不同仓库并行、部分获取回收、外层 cycle 持锁与异常释放。
- `tests/test_git_safety.py`、`tests/test_two_repo_publish_integration.py`：临时 bare remote 上的 fast-forward/ahead/diverged 状态、push 失败保留本地 SHA、双仓库 commit-all、部分 push 停止和同 SHA 恢复。
- `tests/test_update_staging_integration.py`：真实 suite-user/information、compact alias、i18n handler、JSON 校验和 `read_bytes()` 正式树快照。
- 阶段验证（2026-07-18）：Phase 4 聚焦/集成测试 112 passed；全套 `uv run pytest -q`：244 passed；`uv run ruff check`：pass；`git diff --check`：clean。
- Gate 5 验收证据（2026-07-22）：Oracle **APPROVE**。调度器显式使用 `Asia/Tokyo` 触发；持久化 Tokyo daily due marker 覆盖迟到、合并、重启和重叠触发，并仅在成功且日期匹配时完成标记。仓库采用 clone/open `flock`；仅实现 Strapi ID outbox（**不包含 `event_tracker`**），投递前持久化，只有 Git 事务完成或可恢复 readiness checkpoint 后才进入 ready，随后执行 Git 后 HTTP 投递；使用 header auth，并支持去重与重试。验证：`uv run pytest -q` **377 passed**；聚焦测试 **120 passed**；Ruff 与 `git diff --check` 均 clean。

### 执行记录

- 2026-07-18：在分支 `fix/phase-4-update-git-safety` 完成阶段 4。统一 scheduler cycle 在 04:00 保留 daily/full-refresh 语义、普通周期执行版本 gate；增加进程内非阻塞锁和按规范化路径排序的跨进程 `flock`。Git 更新改为显式 fetch/fast-forward/pending-push 状态机，push 失败保留本地提交并由后续周期恢复，禁止删除或重克隆已有仓库。
- 2026-07-18：生成流程改为 repository-adjacent staging、JSON 重新解析校验、文件级 `os.replace` 发布；所有 master 非版本文件和 i18n 文件完成后，最后发布 master `versions.json`，仅成功后推进 published `version_info`。publication 失败清理两个 staging root，但保留已替换的 dirty 工作树供诊断；明确接受这不是多文件事务或跨仓库 2PC。
- 2026-07-18：commit 仅使用 cycle manifest；所有仓库先 commit 再按固定顺序 push，首个 push 失败停止后续 push，保留所有 pending local SHA。通过本地 bare remote、spawn 锁和真实 staging 路径完成验收；未访问生产仓库或外部网络。
- 2026-07-18：为后续“一小时硬期限与 04:00 daily 抢占”增加严格 owner metadata 基础（尚未接入生产）：包含 canonical schema、Linux `/proc` 身份字段、0600 原子 owner 文件、完整 metadata matched-delete、多锁写失败回滚和持有 `flock` 期间的 cleanup 协议。该提交不创建或终止 worker，不改变 scheduler 行为；owner/watchdog、进程树终止和 daily 抢占必须在 Linux CI/隔离环境完成真实信号验证后另行接入。
- 2026-07-22：完成 Gate 5 Phase 4 执行记录并获 Oracle **APPROVE**。新增显式 `Asia/Tokyo` scheduler trigger、可持久化且按日期绑定的 Tokyo daily due marker（覆盖 late/coalesced/restart/overlap，成功后才完成），clone/open `flock`，以及仅限 Strapi ID 的持久化 outbox：Git 事务完成或可恢复 readiness checkpoint 后 ready，Git 后 HTTP、header auth、dedupe/retry。`event_tracker` outbox 仍属于阶段 7，未宣称完成。全套 `uv run pytest -q` 377 passed，聚焦 120 passed，Ruff 与 `git diff --check` clean。
- 2026-07-23：阶段 4 通过 [PR #9](https://github.com/Sekai-World/sekai-client/pull/9) 合并到 `transform-python`，合并提交为 `<commit-id>`。PR #9 包含更新周期互斥、Git 发布恢复、staging 原子发布、协作式普通周期 deadline，以及仅限 Strapi ID 的持久化 outbox；`event_tracker` 排名 outbox 仍未实现，保留在阶段 7。

## 阶段 5：区域 Bootstrap 与客户端状态机

### 目标

使用每区域生命周期状态替代全局 `bootstrapped` 和不完整的 `is_init` 语义。

当前状态：`[-]` 已实现区域生命周期、readiness/liveness 和目标区域请求门控；生产 PM2/Gunicorn 验证、单区域 canary、实际公共入口与部署监控尚未完成。因此本阶段不标记为生产验收完成。

### 建议状态

```text
UNINITIALIZED
→ INITIALIZING
→ READY
→ DEGRADED
→ REAUTHENTICATING
→ FAILED
```

每个区域至少记录：初始化状态、认证状态、最近错误、最近尝试时间和下次重试时间。

### 任务

- [x] 定义每区域生命周期状态与合法状态转换。
- [x] 移除或废弃全局 `bootstrapped` 布尔语义（保留只读兼容符号，不再作为授权或初始化依据）。
- [x] 请求仅初始化目标区域，不串行等待全部区域（公共 API 使用目标区域 `ensure_ready`）。
- [x] 初始化失败按区域独立退避重试。
- [x] readiness 同时检查初始化和登录状态。
- [x] 每个正式区域进程在启动时固定 region；PM2 模板显式声明 JP/EN/TW/KR 拓扑。
- [x] 不提供运行时区域切换：不同 region 的 `init()` 被拒绝；同 region `init()` 是幂等 no-op，生命周期变更及兼容 RPC 通过同一锁序列化。
- [x] 分离 liveness 和 readiness。
- [x] 记录状态转换和失败原因，便于 Dashboard 展示与排障。

以下边界仍未完成：不把兼容性的纯 `bootstrap()` 入口误记为生产 bootstrap 验收；尚未完成受控 PM2/Gunicorn 验证、单区域 canary/rollout、真实公共部署与监控检查。阶段 5 之外的 bootstrap 任务，以及阶段 6/7 项目，也不因本次代码切片而变更状态。

### 验收条件

- [x] 一个区域失败不会阻塞其他区域。
- [x] 初始化成功但登录失败不会被标记为 READY。
- [x] 不会跨区域复用用户状态：不同 region 初始化被拒绝，同 region 重复初始化不改变已提交状态。
- [x] 同一区域不会同时运行两次初始化。
- [x] readiness 能准确返回每个区域不可用原因。

生产验收条件仍未完成：受控 PM2/Gunicorn 验证、单区域 canary/rollout、实际公共入口/部署监控检查。

### 验收证据

- `tests/test_shared_client.py`、`tests/test_api_client.py`：覆盖固定区域、生命周期状态转换、序列化初始化/恢复、退避重试、状态快照与兼容 RPC 边界。
- `tests/test_api_public_server.py`：覆盖目标区域 `ensure_ready`、未就绪时脱敏 503 与 `Retry-After`、区域隔离，以及只读 readiness/liveness 和 legacy `/health` 兼容行为。
- `tests/test_service_dashboard.py`、`tests/test_shared_client_deployment.py`：Dashboard 使用 readiness（不使用 init）并验证正式 JP/EN/TW/KR PM2 模板显式 `--workers 1`、loopback 绑定和区域拓扑。
- Oracle Gate 1：**APPROVE**；Oracle Gate 2：**APPROVE**。
- 全套验证：`pytest` **414 passed**；Ruff、Mypy 和 `git diff --check`：clean。
- 尚未提供：受控 PM2/Gunicorn 验证、单区域 canary/rollout、真实公共入口与部署监控证据。

### 执行记录

- 2026-07-23：提交 `<commit-id>`（`feat: add fixed-region client lifecycle`）实现每进程固定区域、区域生命周期状态、序列化生命周期变更与兼容 RPC、纯 readiness/liveness 状态接口及独立退避；提交 `<commit-id>`（`feat: expose regional lifecycle readiness`）实现公共 API 目标区域 `ensure_ready`、脱敏 503/`Retry-After`、`/health/live`、`/health/ready` 和 legacy `/health` 兼容，并将 Dashboard 探针改为 readiness、PM2 正式区域模板显式 `--workers 1`。两提交目前为 `transform-python` 上未推送的阶段 5 工作；无 PR/合并状态可记录。
- 本次代码验收：Oracle Gate 1/2 均 **APPROVE**；全套 `pytest` 414 passed，Ruff、Mypy、`git diff --check` clean。生产 PM2/Gunicorn、canary/rollout、公共部署和监控验收仍待完成。
- 2026-08-14: Added a separate TW remote-account-provider PM2 canary template,
  configuration contract test, and backup/observation/rollback runbook. This is
  preparation only; no production process was changed and canary acceptance
  remains pending.

## 阶段 6：Deadline、重试与队列生命周期

### 目标

让 RPC、队列、HTTP 和重试共享一个端到端时间预算，并防止调用方超时后任务继续提交状态。

### 任务

- [x] 为每次 RPC 创建绝对 deadline。
- [x] 将剩余预算传递给队列等待、HTTP 请求和 retry。
- [x] 保证现有重试总时间受 RPC deadline 限制。
- [x] 只自动重试明确幂等的操作。
- [x] 有副作用的操作需要幂等键，并在重试中保持同一 request ID。
  - 接受边界：当前游戏 API 写请求默认不重试，因此不需要幂等键；未来账号租约写接口必须由 `AccountProvider.acquire` 接受调用方生成且跨重试稳定的 `idempotency_key`。
- [x] 使用指数退避、jitter 和服务端 `Retry-After`。
- [x] 调用方放弃后，任务不得最终提交登录、session、用户或版本状态。
- [x] 429 等待不得无限占用唯一 worker。
- [x] 记录队列长度、等待时间、执行时间、拒绝数和超时数。
- [x] 评估队列容量与快速拒绝策略，但暂不盲目增加 Gunicorn worker。

### 验收条件

- 任意任务总执行时间不超过其 deadline。
- 调用方超时后任务不会继续提交客户端状态。
- 非幂等请求不会因网络异常自动重复执行。
- 429 不会无限占用 worker。
- 队列满时快速返回明确、可重试的错误。

### 验收证据

- `tests/test_deadline.py`：monotonic deadline、预算 header、HTTP timeout 截断和退避不得越过 deadline。
- `tests/test_shared_client.py`：迟到普通任务、初始化与隐藏认证 callback 不得提交状态；queue-full 稳定契约与 readiness 指标。
- `tests/test_api_client.py`：GET 临时失败重试、写请求不重试、稳定 request ID、429/`Retry-After`、非临时 4xx 不重试。
- `tests/test_task_queue.py`：过期任务执行前丢弃、指标快照与兼容 worker 行为。
- PRs [#21](https://github.com/Sekai-World/sekai-client/pull/21)、[#22](https://github.com/Sekai-World/sekai-client/pull/22)、[#23](https://github.com/Sekai-World/sekai-client/pull/23)、[#24](https://github.com/Sekai-World/sekai-client/pull/24) required CI 均通过并合并。

### 执行记录

- 2026-08-13：完成 Phase 6 PR 1 代码切片。JSON-RPC client 通过内部 header 发送相对预算，shared client 在接收端转换为本机 monotonic 绝对 deadline，并让入队等待、结果等待、worker 上下文、游戏 HTTP、版本探测与现有 429 等待共享剩余预算。跨进程不传绝对时间，避免依赖时钟同步；服务端预算不超过自身 `ANSWER_QUEUE_TIMEOUT`。新增 `tests/test_deadline.py` 覆盖 monotonic 预算、timeout 截断、header、无效预算和游戏 HTTP 传播。验证：Ruff、scoped Mypy、`pytest tests/ -q`（478 passed）与 `git diff --check` 全部通过。状态提交保护、幂等策略和队列指标仍属于后续独立 PR。
- 2026-08-13：完成 Phase 6 PR 2 代码切片。每个序列化 client job 在执行前保存完整 client/runtime 快照，返回前再次检查 deadline；迟到任务会恢复 client、session、version、user、生命周期错误/退避和 auth generation，初始化任务也不能用迟到 candidate 替换已提交 client。隐藏认证 callback 在 deadline 过期后不再发布生命周期状态。新增测试覆盖迟到普通任务、迟到初始化和迟到隐藏认证 callback。验证：Ruff、scoped Mypy、`pytest tests/ -q`（481 passed）与 `git diff --check` 全部通过。强制终止正在阻塞的线程不在边界内；任务在安全返回接缝执行回滚。
- 2026-08-13：完成 Phase 6 PR 3 代码切片。以 `RetryPolicy.NEVER` / `IDEMPOTENT` 替代布尔重试开关；GET 默认可重试，POST/PUT/PATCH 默认不重试，避免网络结果不确定时重复写入。仅网络异常、429、临时 5xx 与既有可恢复协议响应可进入重试；同一逻辑调用保持同一 request ID。退避使用有上限的指数基数和 jitter，支持数值秒及 HTTP-date `Retry-After`，所有等待均受共同 RPC deadline 限制，429 状态通过 `finally` 清理。验证：Ruff、scoped Mypy、`pytest tests/ -q`（487 passed）与 `git diff --check` 全部通过。带幂等键的写操作仍未实现，留给远程账号服务契约。
- 2026-08-13：完成 Phase 6 PR 4 代码切片。正式队列项使用带 deadline 与入队时间的 `QueuedJob` envelope，worker 在执行前丢弃已过期任务；保留旧 tuple 兼容测试/内部调用。新增线程安全的进程内累计指标：depth、capacity、accepted/rejected/timed-out/expired-before-start/completed、累计排队和执行秒数，并通过 lifecycle/readiness RPC 暴露无敏感信息快照。队列满返回稳定结构 `{code: queue_full, retryable: true, retry_after: 1}`，继续保持容量 1 和单 worker，不用扩 worker 掩盖串行状态约束。验证：Ruff、scoped Mypy、`pytest tests/ -q`（490 passed）与 `git diff --check` 全部通过。

### 子项 A：协作式更新周期 Deadline（已落地，独立于 RPC/队列 deadline）

本子项实现阶段 6 中“更新周期时间预算”的最小、协作式（非强制）版本，仅作用于 `check_update._run_update_cycle` 的单一更新周期，不接入 PM2 进程树、不发送信号、不终止 worker、不做 daily 抢占。

#### 已接受的边界（Accepted Boundary）

- **普通周期默认预算**：`DEFAULT_ORDINARY_DEADLINE_SECONDS = 3600` 秒（1 小时）。可通过 `_run_update_cycle(daily=False, deadline_seconds=...)` 或显式 `Deadline` 由测试/调用方控制；不新增环境变量配置（沿用既有函数参数/默认值机制）。
- **协作式检查点（cooperative checkpoints only）**：deadline 仅在安全接缝处检查，绝不中断正在进行的原子操作。检查点顺序为：
  1. 现有 maintenance / candidate 门控（`_cycle_should_proceed`）**之后**；
  2. 任意仓库/网络准备**之前**（首个准备前接缝）；
  3. 每个 `prepare_repo_for_update` **之前**；
  4. prepare 与 generation **之间**（昂贵网络 fetch + staging 生成之前）；
  5. commit **之前**；
  6. push **之前**。
- **不强制终止（no forced termination）**：deadline 过期只让周期在下一个安全接缝返回稳定状态 `deadline_exceeded`；不杀进程、不抛信号、不调用 `os.kill`、不触发任何跨 PM2 抢占（no cross-PM2 preemption）。`_publish_staging` 与单个 `os.replace` 原子替换内部**不检查** deadline，保证发布原子性不被打断。
- **被阻塞调用可能超过预算（blocked calls may exceed budget until next seam）**：若某个 prepare / 网络 fetch / commit 在检查点之间被阻塞（例如慢网络、被 `flock` 等待），该调用允许运行到完成，周期仅在**下一个**检查点发现过期；预算是协作上界而非硬实时上界。
- **daily 禁用 deadline（daily disables the deadline）**：`daily=True` 时 deadline 强制为 `None`，daily/full-refresh 周期永不被协作式取消；即使调用方显式传入 `Deadline` 也会被忽略。
- **锁释放语义不变**：`deadline_exceeded` 经由现有 `finally` 释放进程内锁与仓库 `flock`，不回滚本地 commit，不改变发布/push 安全行为；后续周期可正常获取锁。

#### 验收证据（协作式 deadline）

- `tests/test_check_update.py`：`Deadline` 单元测试——`None` 禁用永不过期、有限非负秒构建、零秒立即过期、拒绝负数/无穷/非数字；通过 monkeypatch `check_update._monotonic` 验证间隔后过期（无 sleep）。
- `tests/test_update_cycle_safety.py`：
  - `test_ordinary_expired_deadline_returns_deadline_exceeded`：普通周期过期 deadline 返回 `deadline_exceeded` 且不进入昂贵生成阶段。
  - `test_daily_ignores_expired_deadline_and_proceeds`：daily 忽略过期 deadline 并继续完成。
  - `test_deadline_exceeded_releases_outer_process_lock_and_repo_flock`：deadline 路径释放进程锁 + 仓库 flock，后续周期可获取并完成。
  - `test_deadline_exceeded_before_commit_skips_push`：确定性 deadline 替身（无 sleep）放行门控/prepare/generation 接缝、仅在 pre-commit 接缝抛出；证明 generation 已发生而 commit/push 未发生。
  - `test_deadline_disabled_for_daily_even_if_passed`：显式传入过期 deadline 时 daily 仍禁用并继续。
- 验证（2026-07-19）：`ruff check` 通过；`pytest tests/test_check_update.py tests/test_update_cycle_safety.py tests/test_update_cycle_lock_integration.py`：84 passed。

## 阶段 7：Event Tracker Outbox 与 API 响应校验

### 目标

避免排名快照因短暂网络故障永久丢失，并减少上游 schema 变化导致的运行时崩溃。

### 任务

- [x] 设计 SQLite outbox，幂等键使用 `(region, event_id, timestamp, data_type)`。
- [x] 支持 `pending`、`sending`、`sent` 和 `failed` 状态。
- [x] 排名数据先持久化，再 POST。
- [x] POST 成功后标记完成，失败后退避重试。
- [x] 进程重启后继续处理未完成 outbox。
- [x] 每轮投递有 30 秒预算，单次远端请求超时为 15 秒，避免阻塞后续调度。
- [x] terminal `sent`/`failed` 记录按可配置的默认 24 小时保留期清理，不删除 pending/sending。
- [x] Scheduler 使用 `max_instances=1` 和 `coalesce=True`。
- [x] 移除通过删除、重新添加 job 处理慢任务的逻辑。
- [x] 记录任务执行时间和 outbox 状态/积压；精确调度延迟指标保留在性能 roadmap 阶段 0。
- [ ] 从登录响应开始引入明确的响应模型和边界校验。
- [ ] 依次覆盖版本、活动、ranking 和 master 数据响应。
- [ ] schema 错误不得破坏当前有效客户端状态。

### 验收条件

- POST 临时失败不会丢失排名快照。
- 进程重启后可以继续发送。
- 重复处理不会产生重复数据。
- 上游 schema 变化产生明确、可诊断的解析错误。
- 解析失败不会覆盖当前有效状态。

### 验收证据

- `tests/test_event_outbox.py`：覆盖 enqueue 幂等、私有文件权限、成功确认、临时失败退避、重启恢复、过期 claim 恢复、失败终态与章节幂等键。
- `tests/test_event_outbox.py`：另覆盖 terminal retention 和 drain 时间预算。
- `tests/test_event_tracker.py`：覆盖单一 coalescing scheduler job、先持久化、携带幂等键投递和独立投递状态日志。
- 客户端 outbox 已完成；接收 API 尚未持久化或强制执行 `Idempotency-Key`，因此 crash-after-remote-commit 边界的端到端去重验收仍待接收端配套变更。
- 待填写：响应 schema 异常测试

### 执行记录

- 待填写

## PR 拆分

| PR | 建议标题 | 范围 | 状态 | 链接 |
|---|---|---|---|---|
| 1 | `fix: validate supported region configuration` | CN 决策、区域配置校验与测试 | `[x]` | #5 merged |
| 2 | `ci: establish phase 1 verification baseline` | Ruff、mypy、pytest CI | `[x]` | [#6](https://github.com/Sekai-World/sekai-client/pull/6) merged（`<commit-id>`） |
| 3 | `security: harden internal rpc and credential handling` | 日志、token、文件权限、RPC | `[x]` | [#7](https://github.com/Sekai-World/sekai-client/pull/7) merged（`<commit-id>`） |
| 4 | `fix: harden dashboard rendering and restart actions` | 状态、确认、DOM、token | `[-]` | 分支 `security/phase-3-dashboard-safety`，待浏览器验收与 PR |
| 5 | `fix: harden update cycle and git publishing` | 定时任务互斥、Git 发布恢复、staging 原子发布、协作式普通周期 deadline，以及仅限 Strapi ID 的持久化 outbox（不含 `event_tracker` 排名 outbox） | `[x]` | [#9](https://github.com/Sekai-World/sekai-client/pull/9) merged（`<commit-id>`，2026-07-23） |
| 6 | `refactor: model per-region client lifecycle` | 区域状态机、bootstrap、readiness | `[-]` | 未提交阶段 5 工作：`<commit-id>`、`<commit-id>`；无 PR |
| 7 | `refactor: enforce request deadlines and retry policy` | Deadline、重试、取消、队列 | `[ ]` | 待填写 |
| 8 | `feat: persist event tracker delivery outbox` | SQLite outbox 与 scheduler | `[ ]` | 待填写 |
| 9 | `refactor: validate upstream api responses` | 关键 API 响应模型 | `[ ]` | 待填写 |

## 发布与回滚原则

- 每个 PR 独立上线和回滚，不创建覆盖所有阶段的大 PR。
- 先补相应测试，再修改行为。
- 先修数据丢失和凭据泄露，再做架构优化。
- 不同时重构状态机和重试系统。
- 不将 gevent 或增加 worker 数作为队列问题的直接修复。
- 数据发布改动先在临时仓库验证。
- 登录和写接口改动先在单一区域灰度。
- 阶段 5-7 上线后重点观察错误率、队列等待、任务耗时、区域 readiness 和 outbox 积压。

## Current Repository Audit (2026-08-13)

This audit reflects the current `main` working tree. Historical branch, commit,
and test-count records above are retained as execution history.

- Phases 2 and 4 are implemented with repository tests and remain complete.
- Phase 0 is complete: topology, loopback binding, worker count, and all master
  and i18n repository states were confirmed in production. High historical EN
  restart counters remain a non-blocking observation; both processes are
  currently stable with zero unstable restarts.
- Phase 1 is complete. Before publication, PR CI was moved from the persistent
  self-hosted runner to `ubuntu-latest`, action references were pinned to commit
  SHAs, Gitleaks 8.30.1 scanned all four Git commits with zero findings, and the
  GitHub-hosted push CI passed. The repository is public and the active
  `protect default` ruleset strictly requires
  `Lint, type-check and test` on pull requests. PR #19 verified enforcement
  end-to-end without administrator bypass.
- Phase 3 code is present on `main`; desktop/mobile browser and real PM2 restart
  acceptance evidence is still missing.
- Phase 5 lifecycle and readiness code is present on `main`; the older
  "uncommitted" wording above is stale. The TW remote-provider canary and its
  first monitoring window are now recorded below. Public endpoint verification
  and expansion to another region remain open.
- Phase 6 is incomplete. Only the cooperative `check_update` cycle deadline is
  implemented; end-to-end RPC deadlines, cancellation, idempotency-aware
  retries, `Retry-After`/jitter handling, and queue metrics remain open.
- Phase 7 is not implemented. The existing Strapi ID outbox belongs to the
  update publisher and does not satisfy the event-ranking outbox requirement.
  `event_tracker` still removes and recreates a slow scheduler job, and explicit
  upstream response models remain absent.
- Production Git authentication migration is prepared in-repository: the
  `repository-scoped-github-app` GitHub App is restricted to the six generated-data
  repositories, and the credential helper replaces long-lived PATs with
  repository-scoped installation tokens. Production installation, old-token
  revocation, and update-cycle verification remain operational steps.
- [architecture-decoupling-roadmap.md](architecture-decoupling-roadmap.md) tracks
  the subsequent modularization and account-service extraction. Its remote
  provider rollout depends on the unfinished Phase 6 reliability work.
- Follow the roadmap's
  [Recommended Execution Order](architecture-decoupling-roadmap.md#recommended-execution-order):
  finish the Phase 0/1 confirmations first, then the critical Phase 6 work;
  local account-provider refactoring may begin before unrelated remediation
  items are complete, while remote rollout requires Phase 5 production
  acceptance.

## 进度日志

按日期追加简短记录，包含完成事项、验证结果、阻塞项和下一步。

| 日期 | 阶段 | 记录 | 下一步 |
|---|---|---|---|
| 2026-07-16 | 0 | D-001 决策：CN 非正式区域，保留简化版 checkUpdate-cn。代码层：REGIONS/PM2/公共 API 移除 CN，新增区域映射完整性校验与聚焦测试。生产运行事实（PM2 进程、loopback 绑定、worker 数、未推送 commit）待运维确认。 | 阶段 1 测试基线与 CI；运维核对生产事实 |
| 2026-07-16 | 1 | 新增 CI（`.github/workflows/ci.yml`，针对 transform-python，read-only，Python 3.12 + uv）。机械 lint 修复 tests/ 与 service_dashboard.py（零逻辑变化）。新增 5 个回归测试：登录缓存、bootstrap fail-fast 安全边界、队列满拒绝、push 失败返回契约。4 项原任务因依赖阶段 4/5/6 标记为 deferred（bootstrap 部分失败恢复、POST 非幂等重试、update job 互斥、push 失败保留本地数据）。验证：format/check/mypy/pytest(76) 全绿，`git diff --check` clean。PR #6 已合并为 `<commit-id>`；required status check 的仓库设置仍待确认。 | 阶段 2；运维核对阶段 0 生产事实和分支保护 |
| 2026-07-16 | 2 | 实现最小安全设计：日志脱敏、Strapi header 化、凭据 YAML 原子写入 0600、内部 RPC 鉴权、请求白名单、受限 master split RPC、account_info 缩权、PM2 env 传递及安全 PM2 示例。最终 pytest 119，format/check/mypy/node/git diff --check 全绿。PR #7 已合并为 `<commit-id>`。**延期（未伪称完成）**：secret store、mTLS/Unix socket、完整 capability model；Dashboard token 存储转入阶段 3。 | 阶段 3 Dashboard 安全与交互 |
| 2026-07-17 | 3 | 已实现统一状态模型、安全 DOM 渲染、重启确认与防重复、结构化 restartStatus、session-only/记住设备/清除 token。聚焦测试 24 passed，实施时全套测试 139 passed，JavaScript syntax 与 diff check 通过。真实桌面/移动端流程和 PM2 重启尚未手工验证。 | 完成浏览器验收，更新证据后提交阶段 3 PR |
| 2026-07-22 | 4 | Gate 5 Oracle **APPROVE**：显式 `Asia/Tokyo` scheduler trigger；持久化 Tokyo daily due marker 覆盖 late/coalesced/restart/overlap，且仅成功并完成日期绑定时清除/完成；clone/open `flock`；仅 Strapi ID outbox（不含 `event_tracker`），先持久化，Git 事务完成或可恢复 readiness checkpoint 后 ready，再 Git 后 HTTP，header auth、dedupe/retry。验证：`uv run pytest -q` 377 passed，聚焦 120 passed，Ruff 与 `git diff --check` clean。 | 阶段 5；阶段 7 event_tracker outbox 仍未完成 |
| 2026-07-23 | 4 | PR [#9](https://github.com/Sekai-World/sekai-client/pull/9) 已合并到 `transform-python`，合并提交 `<commit-id>`。合并范围为更新周期互斥、Git 发布恢复、staging 原子发布、协作式普通周期 deadline，以及仅限 Strapi ID 的持久化 outbox；不包含 `event_tracker` 排名 outbox。 | 阶段 5；阶段 7 event_tracker outbox 与 API 响应校验仍未完成 |
| 2026-07-23 | 5 | 未推送的 `<commit-id>`、`<commit-id>` 实现固定区域生命周期、序列化状态变更、纯 readiness/liveness、目标区域 `ensure_ready` 与脱敏 503/`Retry-After`、live/ready/legacy health、Dashboard readiness 以及正式 JP/EN/TW/KR 的 PM2 `--workers 1` 拓扑。Oracle Gate 1/2 均 **APPROVE**；全套测试 414 passed，Ruff/Mypy/diff check clean。 | 受控 PM2/Gunicorn、单区域 canary/rollout、真实公共部署与监控检查；未实现的 bootstrap、阶段 6/7 保持待办 |
| 2026-08-16/17 | 5 | The first TW remote-provider consumer passed the 24-hour gate with the client and account service online and ready, without observed process or container restarts. Lease and inventory health stayed within acceptance criteria; no account-service error, failure, or quarantine signal was observed in the operator snapshot. Exact operational identifiers and counters are retained privately. The event-ranking SQLite outbox was not part of this canary. | TW gate accepted. Verify the next region's inventory and configuration before one-region-at-a-time rollout; retain rollback artifacts through the later of 24 hours after activation or one scheduled update cycle. |
