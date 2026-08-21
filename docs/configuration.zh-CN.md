# 配置与 profile

配置 schema v11 的合并顺序是：内置 `codex-app-server-default`、manifest 指定的项目
`autonomous/config.yaml`、显式 `--profile`、可选的 launcher 一次性覆盖。最后执行核心可信边界校验，因此项目或
用户 profile 都不能移除核心 protected paths、关闭 final claim 独立审计、替换持久
controller、开放机械子工网络或启用 fast/priority/auto tier。

以下命令只读、自动脱敏、不会启动模型：

```console
amr config validate --project ./research-target
amr config explain --project ./research-target --profile ./my-profile.json
amr config summary --project ./research-target
```

每个角色可独立声明 provider、model、endpoint/profile、effort 及不支持时的显式
策略、service tier、输出规范化、timeout、两类 retry、concurrency、token limit、
cost limit 和 estimated cost。默认仍全部走 Codex App Server；API 只在显式路由后
使用。

v9 新增了 `campaign.hours`（默认 12）和 `campaign.epoch_hours`（默认 2）。`amr run`
未传对应参数时读取项目值；显式 `--hours`、`--epoch-hours` 保持最高优先级。
`config summary` 会脱敏输出项目、campaign、并发、预算、逐角色路由和机械路由摘要。

`engine` 还声明同线程研究 continuation：`research_max_turns.prover`、
`.falsifier`、`.explorer` 分别配置，内置默认均为 12；
`reasoning_health_short_tokens` 默认 600；
`reasoning_health_repeated_token_tolerance` 默认 2；
`reasoning_health_retry_limit` 默认 2。它们只控制诊断、有限重试和 provider 明确支持时的
`xhigh -> max` 升级，不能改变数学状态、trust、evidence 或 audit 结论。harness 不设置
App Server active goal；per-thread token 限额继续由 controller 根据 telemetry 执行。
模型首次返回 `BLOCKED` 时必须先进行一次 controller 管理的同线程修复 turn。只有修复后
再次给出结构完整、可供调度的 blocker，才允许结束逻辑任务；这一验证只影响执行调度，
不改变数学、trust 或 evidence 状态。达到 turn 或 controller token 边界但没有验证进展时，
harness 会生成仅供研究续接的非 canonical 检查点，记录当前 obligation、已绑定证据和下一
obligation，并延后到下一 epoch；不会把路线记为失败，也不会重置 stagnation。下一 epoch
调度前会重新校验检查点摘要和完整 continuation 任务包。

新项目主角色默认额度为 5 亿 token；机械子工独立额度为 15 亿。机械席位上限默认
`null`，只表示没有静态数量上限，broker 仍受预算、系统资源、rate limit、队列、
dispatch batch、超时和 operator stop 约束。策略可选 `preferred`、`balanced`、
`conservative`、`disabled` 或带阈值的 `custom`。

token telemetry 分开记录总 input、cached input、uncached input、cache-write input、output
和 reasoning output。provider 的 total token 继续作为预算权威值；评估单任务深度时应查看
uncached/output/reasoning。机械 Spark 遇到模型不可用、额度、传输或超时时，只会按固定策略
切换一次到配置的 fallback（默认 Luna medium）。策略、权限、任务资格、schema、protocol 和
artifact 错误不得触发 fallback。若 fallback 也出现 `You've hit your usage limit` 等用量耗尽，
则归类为 `provider_quota_exhausted`：campaign 暂停，精确任务或非 canonical 检查点保留到下一
epoch，provider 提供的官方 reset 时间会被保存；它不计入数学失败、路线失败或 stagnation。
只要任一机械尝试缺少完整 token telemetry，监视面板就显示观测下界和未知次数，例如
“机械 ≥0（1次用量未知）”，不能再把它理解为精确的零消耗。

用户 profile 必须只含 `profile_schema_version`、`name`、`extends`、`overrides`，且
不能改项目 ID。v7/v8/v9/v10 项目配置会在内存中迁移到 v11；审核有效配置后可用
`amr config migrate --project PATH --write` 原子写回，且不会启动模型。launcher 的
一次性覆盖只允许简单运行和路由字段；provider capability、credential、protected
paths、audit/trust policy 等必须编辑项目配置并重新通过完整预检。示例见
[`examples/per-role-api-profile.json`](examples/per-role-api-profile.json)。
