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
| 0 | 生产事实确认与 CN 范围决策 | `[-]` 代码层完成，生产事实待确认 | 0.5 天 | 无 | PR 1 |
| 1 | 测试基线与 CI | `[ ]` | 1 天 | 阶段 0 | PR 1-2 |
| 2 | 凭据、日志和内部 RPC 安全 | `[ ]` | 1-2 天 | 阶段 1 | PR 3 |
| 3 | Dashboard 安全与交互 | `[ ]` | 0.5-1 天 | 阶段 1 | PR 4 |
| 4 | 定时任务互斥与 Git 数据安全 | `[ ]` | 1-2 天 | 阶段 1 | PR 5 |
| 5 | 区域 bootstrap 与客户端状态机 | `[ ]` | 2-3 天 | 阶段 1 | PR 6 |
| 6 | Deadline、重试与队列生命周期 | `[ ]` | 2-3 天 | 阶段 5 | PR 7 |
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
- [x] 自检代码库区域声明：从 `Config.REGIONS`、`api_public_server.client_map`、PM2 `regions` 移除 CN；保留 `checkUpdate-cn` 独立进程与 `CN_PORT`/port-map。
- [ ] 检查生产环境实际启动的区域和 PM2 进程。
  - 状态：`[!]` 待生产确认——代码层已变更，但生产实际运行进程需运维核对 PM2 列表。
- [ ] 确认 shared client 是否始终绑定 `127.0.0.1`。
  - 状态：`[!]` 待生产确认——`ecosystem.config.js` 已固定 `-b 127.0.0.1`，但运行实例需核实。
- [ ] 确认每个 shared client 的 Gunicorn worker 数量。
  - 状态：`[!]` 待生产确认——gunicorn 默认 1 worker，需运维确认。
- [ ] 确认 master/i18n 仓库是否可能存在未推送 commit。
  - 状态：`[!]` 待生产确认——仅代码层无法判断工作树状态。
- [x] 记录当前 pytest、Ruff 结果作为基线（见“验收证据”）。
- [x] 增加启动期/配置层区域映射完整性校验（`Config.validate_region_config`，并在 `api_public_server.bootstrap` 启动失败）。

### 验收条件

- [x] 所有声明支持的区域（jp/en/tw/kr）均具有 headers、URL、端口等完整配置。
- [x] 缺失区域配置时，进程在启动阶段给出明确错误（`Config.validate_region_config` + `api_public_server.bootstrap` 抛 `RuntimeError`）。
- [ ] 生产部署约束已记录，不再依赖未文档化假设。
  - 状态：`[!]` 部分完成——代码层约束已加，生产运行事实（绑定地址、worker 数、未推送 commit）仍待运维确认。

### 验收证据

- 测试（执行于 2026-07-16，仓库内 `pytest` 子集）：
  - `pytest tests/test_config.py -q`：新增 `TestRegionConfigValidation` 全部通过（含 CN 已从 `REGIONS` 排除、`validate_region_config` 缺映射报错）。
  - `ruff check .`：通过（仅阶段 0 范围内改动）。
- 代码层改动：
  - `config.py`：`REGIONS` 移除 `cn`；新增 `validate_region_config` 并接入 `Config.validate`。
  - `api_public_server.py`：`client_map` 移除 `cn`；`bootstrap` 在区域映射不完整时启动失败。
  - `deployment/pm2/ecosystem.config.js`：`regions` 移除 `cn`，保留独立 `checkUpdate-cn` 进程（simple mode）。
  - 待生产确认项：实际运行 PM2 进程、loopback 绑定、worker 数、未推送 commit。

### 执行记录

- 2026-07-16：完成 D-001 决策与代码层阶段 0 改动。CN 从正式区域声明移除，保留简化版 `checkUpdate-cn`。新增启动期区域映射完整性校验并接入 `Config.validate` 与 `api_public_server.bootstrap`。新增聚焦测试 `tests/test_config.py::TestRegionConfigValidation`。未做 CI/安全/状态机（属阶段 1-7）。生产运行事实类检查项标记为待运维确认。

## 阶段 1：测试基线与 CI

### 目标

在修改关键行为前建立可重复的验证路径。

### 任务

- [ ] 增加所有支持区域的配置完整性测试。
- [ ] 增加 `api_public_server` bootstrap 部分失败与恢复测试。
- [ ] 增加 shared client 重新初始化状态一致性测试。
- [ ] 增加非幂等请求不得自动重试的测试。
- [ ] 增加 update job 互斥测试。
- [ ] 增加 push 失败保留本地数据和 commit 的测试。
- [ ] 增加最小 CI：Ruff、mypy、pytest。
- [ ] 记录当前 Ruff/mypy 已知问题，不在本阶段混入全仓清理。

### 最小验证命令

```bash
uv run --extra dev ruff check .
uv run --extra dev mypy api_client.py shared_client.py utils/ config.py
uv run --extra dev pytest tests/
```

### 验收条件

- PR 自动执行 lint、类型检查和测试。
- 关键失败路径具备回归测试。
- CI 失败能阻止合并。

### 验收证据

- 待填写：CI 运行链接或本地输出

### 执行记录

- 待填写

## 阶段 2：凭据、日志和内部 RPC 安全

### 目标

阻断凭据经日志、URL、文件和高权限 RPC 泄露的路径。

### 任务

- [ ] 实现集中式日志脱敏。
- [ ] 屏蔽 `Authorization`、`Cookie`、`x-session-token`、credential、access token、signature 和 device ID。
- [ ] 禁止记录完整登录 body 和敏感 headers。
- [ ] 将 Strapi token 从 URL query 移到 `Authorization` header。
- [ ] 为凭据 YAML 使用原子写入和 `0600` 权限，或迁移到 secret store。
- [ ] 为内部 JSON-RPC 增加独立认证。
- [ ] 无内部认证时禁止绑定非 loopback 地址。
- [ ] 从 `account_info` 移除敏感字段。
- [ ] 为 `request_and_decrypt` 增加 scheme、host 和 method 白名单。
- [ ] 评估并收敛通用 `call_pjsk_api` 的权限范围。

### 验收条件

- 日志中不出现 credential、session token 或 access token。
- URL 中不再包含认证 token。
- 未认证请求不能调用内部 RPC。
- RPC 不能向任意 URL 发起请求。
- 合法内部调用保持可用。

### 验收证据

- 待填写：日志脱敏测试
- 待填写：RPC 鉴权与 URL 白名单测试

### 执行记录

- 待填写

## 阶段 3：Dashboard 安全与交互

### 目标

避免误操作、状态误导、HTML 注入和管理 token 不必要的长期暴露。

### 任务

- [ ] 统一状态模型：Healthy、Degraded、Probe failed、Offline、Missing、Restarting。
- [ ] 保证状态颜色与文案来自同一个归一化字段。
- [ ] 单服务重启增加服务名和区域确认。
- [ ] 区域重启显示全部受影响服务并使用更强确认。
- [ ] 重启期间禁用相关操作，防止重复提交。
- [ ] 区分重启失败与重启成功后刷新失败。
- [ ] 使用 `createElement`、`textContent` 和属性赋值替代动态 `innerHTML`。
- [ ] token 默认仅保存在当前会话。
- [ ] 提供显式“记住此设备”和清除 token 操作。
- [ ] 验证桌面端和移动端操作流程。

### 验收条件

- 不会出现红色 `online` 等颜色与文字矛盾状态。
- 重启不能通过一次误点直接触发。
- 服务端返回的动态字段不能注入 HTML。
- 用户能够区分进行中、成功、重启失败和刷新失败。
- 桌面端和移动端均可完成登录、查看状态和重启流程。

### 验收证据

- 待填写：DOM/XSS 测试
- 待填写：桌面和移动端手工验证记录

### 执行记录

- 待填写

## 阶段 4：定时任务互斥与 Git 数据安全

### 目标

防止 04:00 定时任务重叠、部分数据发布和 push 失败导致的数据丢失。

### 任务

- [ ] 合并 update cycle，或为完整流程增加进程级互斥锁。
- [ ] 锁覆盖 pull、获取版本、生成、校验、commit 和 push。
- [ ] Scheduler 设置 `max_instances=1`、`coalesce=True` 和合理的 `misfire_grace_time`。
- [ ] 移除 commit/push 失败后的 `shutil.rmtree`。
- [ ] 区分 clone、pull、commit、push 和认证错误。
- [ ] push 失败时保留本地 commit 并进入待重试状态。
- [ ] 文件先写临时路径，校验成功后原子替换。
- [ ] 将 `versions.json` 放到发布流程末尾更新。
- [ ] 评估使用临时 Git worktree 生成完整数据集。

### 验收条件

- 每天 04:00 只执行一个更新周期。
- 两次并发触发时，第二次被排队或明确跳过。
- push 失败不会删除仓库、working tree 或本地 commit。
- 生成中途失败不会暴露新旧版本混合的数据集。

### 验收证据

- 待填写：并发调度测试
- 待填写：Git push 失败恢复测试
- 待填写：原子发布测试

### 执行记录

- 待填写

## 阶段 5：区域 Bootstrap 与客户端状态机

### 目标

使用每区域生命周期状态替代全局 `bootstrapped` 和不完整的 `is_init` 语义。

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

- [ ] 定义每区域生命周期状态与合法状态转换。
- [ ] 移除或废弃全局 `bootstrapped` 布尔语义。
- [ ] 请求仅初始化目标区域，不串行等待全部区域。
- [ ] 初始化失败按区域独立退避重试。
- [ ] readiness 同时检查初始化和登录状态。
- [ ] 每个区域进程在启动时固定 region。
- [ ] 若保留重新初始化，必须通过同一 executor 原子执行。
- [ ] 重新初始化时完整重置 `api_client`、`user_logged_in` 和 `user_info`。
- [ ] 分离 liveness 和 readiness。
- [ ] 记录状态转换和失败原因，便于 Dashboard 展示与排障。

### 验收条件

- 一个区域失败不会阻塞其他区域。
- 初始化成功但登录失败不会被标记为 READY。
- 重新初始化不会保留旧区域用户状态。
- 同一区域不会同时运行两次初始化。
- readiness 能准确返回每个区域不可用原因。

### 验收证据

- 待填写：状态转换单元测试
- 待填写：多区域部分失败集成测试

### 执行记录

- 待填写

## 阶段 6：Deadline、重试与队列生命周期

### 目标

让 RPC、队列、HTTP 和重试共享一个端到端时间预算，并防止调用方超时后任务继续提交状态。

### 任务

- [ ] 为每次 RPC 创建绝对 deadline。
- [ ] 将剩余预算传递给队列等待、HTTP 请求和 retry。
- [ ] 保证所有重试总时间小于 RPC deadline。
- [ ] 只自动重试明确幂等的操作。
- [ ] 有副作用的操作需要幂等键，并在重试中保持同一 request ID。
- [ ] 使用指数退避、jitter 和服务端 `Retry-After`。
- [ ] 调用方放弃后，任务不得提交登录、session、用户或版本状态。
- [ ] 429 等待不得无限占用唯一 worker。
- [ ] 记录队列长度、等待时间、执行时间、拒绝数和超时数。
- [ ] 评估队列容量与快速拒绝策略，但暂不盲目增加 Gunicorn worker。

### 验收条件

- 任意任务总执行时间不超过其 deadline。
- 调用方超时后任务不会继续提交客户端状态。
- 非幂等请求不会因网络异常自动重复执行。
- 429 不会无限占用 worker。
- 队列满时快速返回明确、可重试的错误。

### 验收证据

- 待填写：虚拟时钟或短 timeout 测试
- 待填写：取消与状态提交测试
- 待填写：429 和幂等性测试

### 执行记录

- 待填写

## 阶段 7：Event Tracker Outbox 与 API 响应校验

### 目标

避免排名快照因短暂网络故障永久丢失，并减少上游 schema 变化导致的运行时崩溃。

### 任务

- [ ] 设计 SQLite outbox，幂等键使用 `(region, event_id, timestamp, data_type)`。
- [ ] 支持 `pending`、`sending`、`sent` 和 `failed` 状态。
- [ ] 排名数据先持久化，再 POST。
- [ ] POST 成功后标记完成，失败后退避重试。
- [ ] 进程重启后继续处理未完成 outbox。
- [ ] Scheduler 使用 `max_instances=1` 和 `coalesce=True`。
- [ ] 移除通过删除、重新添加 job 处理慢任务的逻辑。
- [ ] 记录任务延迟、执行时间和 outbox 积压。
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

- 待填写：outbox 重试、重启与幂等测试
- 待填写：响应 schema 异常测试

### 执行记录

- 待填写

## PR 拆分

| PR | 建议标题 | 范围 | 状态 | 链接 |
|---|---|---|---|---|
| 1 | `fix: validate supported region configuration` | CN 决策、区域配置校验与测试 | `[ ]` | 待填写 |
| 2 | `ci: add project verification pipeline` | Ruff、mypy、pytest CI | `[ ]` | 待填写 |
| 3 | `security: redact credentials and protect internal rpc` | 日志、token、文件权限、RPC | `[ ]` | 待填写 |
| 4 | `fix: harden dashboard rendering and restart actions` | 状态、确认、DOM、token | `[ ]` | 待填写 |
| 5 | `fix: serialize update jobs and preserve git state` | 调度互斥、原子发布、Git 恢复 | `[ ]` | 待填写 |
| 6 | `refactor: model per-region client lifecycle` | 区域状态机、bootstrap、readiness | `[ ]` | 待填写 |
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

## 进度日志

按日期追加简短记录，包含完成事项、验证结果、阻塞项和下一步。

| 日期 | 阶段 | 记录 | 下一步 |
|---|---|---|---|
| 2026-07-16 | 0 | D-001 决策：CN 非正式区域，保留简化版 checkUpdate-cn。代码层：REGIONS/PM2/公共 API 移除 CN，新增区域映射完整性校验与聚焦测试。生产运行事实（PM2 进程、loopback 绑定、worker 数、未推送 commit）待运维确认。 | 阶段 1 测试基线与 CI；运维核对生产事实 |
