# 配置与 profile

配置 schema v12 的合并顺序是：内置 `codex-app-server-default`、manifest 指定的项目
`autonomous/config.yaml`、显式 `--profile`、可选的 launcher 一次性覆盖。最后执行核心可信边界校验，因此项目或
用户 profile 都不能移除核心 protected paths、关闭 final claim 独立审计、替换持久
controller、开放机械子工网络或请求未固定的 service tier。

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

v9 新增了 `campaign.hours`（默认 5）和 `campaign.epoch_hours`（默认 2）。`amr run`
未传对应参数时读取项目值；显式 `--hours`、`--epoch-hours` 保持最高优先级。
`config summary` 会脱敏输出项目、campaign、并发、预算、逐角色路由和机械路由摘要。

## 研究 policy pack

以下配置片段通过 `policy.pack` 选择一个内置 domain contract：

```json
{
  "policy": {
    "pack": "math-research"
  }
}
```

可选值是 `math-research`、`certified-computational-research` 和
`empirical-research`。内置 profile 与未传 `--domain` 的 `amr init` 都默认
`math-research`，因此现有数学项目保持兼容。`amr init --domain PACK` 会写入同一配置项，
并创建对应域的初始 ClaimGraph。未知名称、非内置目录或 profile 注入的自定义 pack 都会
校验失败。

新 run 会严格校验选中 pack 的 descriptor，并把 descriptor、skill、角色 prompt、
reference、domain contract、audit requirements 和机械资源连同 SHA-256 快照固定到
run-local policy 目录。resume 使用这些已验证快照并报告已安装源码 drift；快照缺失、
被修改或跨 pack 重绑定时 fail closed。详见
[研究域与 policy pack](research-domains.md)。

## Fast 模式

Fast 是默认关闭的单一显式开关，可写在项目配置或 profile：

```json
"execution": {
  "fast_mode": true
}
```

启用后，AMR 为 Director、研究、审计和 smoke 等所有 controller 主角色统一派生
`service_tier: "fast"`，并在 `thread/start` 与每个 `turn/start` 重复固定。只有该开关
明确为 true 时，服务端回报的 `priority` 才作为 Fast 的观测别名接受；关闭时观测到
fast/priority 会 fail closed。逐角色填写 fast/priority/ultrafast 不能绕过开关。机械子工在两种
模式下都继续严格为 `service_tier=null`。

启动 canonical refresh 没有可关闭的配置开关，项目配置或用户 profile 均不能绕过。
`amr run` 在任何模型 turn 之前冻结 manifest 声明的 canonical inputs、SHA-256、可用的
Git revision、结构化 claim/trust mirror 与可选 Director overlay。崩溃 resume 必须继续
使用原冻结输入；新 epoch 遇到安全可同步的 canonical 更新时丢弃旧 pending planning 并
重建动态 snapshot，若仍有无法安全重绑定的 audit frontier 则 fail closed。刷新只写入
run-local derived state，不改写 `CLAIMS.md`、`PROGRESS.md` 或数学/trust 状态。
实时 `ClaimGraph` 是数学状态和 proof frontier 的唯一机器权威；trusted metadata 绑定
ClaimGraph 摘要。Markdown 只有在显式包含 AMR 机器状态块时才参与严格一致性检查，普通
叙述只是上下文。

无人值守跨 epoch 使用显式 CLI 模式：

```console
amr run --project ./research-target --hours 12 --epoch-hours 2 --auto-epochs
```

每个新 epoch 保留独立 checkpoint/seal，并重新执行完整启动刷新。只有普通 epoch 时间
边界会自动续跑；quota pause、fail-closed 状态错误、内部失败、人工停止、数学完成或 campaign
总时长耗尽都会停止循环。原有 `amr campaign continue` 保持兼容，并可增加
`--auto-epochs` 从已封存 checkpoint 继续无人值守运行。

崩溃后可在同一次无人值守命令中先恢复原 epoch，再继续 fresh epoch：

```console
amr run --project ./research-target --resume EPOCH_ID --auto-epochs
```

resume 从原 RUN_MANIFEST 恢复 campaign 归属、时长和绝对 deadline；冲突的时长、
campaign、mode 或 pinned config 覆盖会在 recovery 前 fail closed。pre-recovery 失败只
终止本次 controller attempt，不封存 epoch，也不覆盖 planning snapshot。存在未封存
epoch 时，`amr campaign continue` 会返回准确的 resume 命令。

自动进入下一 epoch 除了要求 checkpoint 可用，还要求 report、不可变文件索引、语义
索引、OUTCOME 和 run summary 已完整提交。监视器会在这一阶段显示成果归档进度，不会
提前报告 run 已结束。

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
不能改项目 ID。v7 至 v11 项目配置会在内存中迁移到 v12；审核有效配置后可用
`amr config migrate --project PATH --write` 原子写回，且不会启动模型。launcher 的
一次性覆盖只允许简单运行和路由字段；provider capability、credential、protected
paths、audit/trust policy 等必须编辑项目配置并重新通过完整预检。示例见
[`examples/per-role-api-profile.json`](examples/per-role-api-profile.json)。
