# Autonomous Math AI

**Autonomous Math AI — 可审计的数学研究 AI 编排工具**

Autonomous Math AI 是一个与具体猜想解耦的 Python harness，用于组织长期运行的
AI 辅助数学研究。它负责研究任务、反证搜索、独立审计、持久化证据、崩溃恢复和
受控机械子工，但不会把模型输出自动当作数学事实。

> Autonomous Math AI 不是“证明预言机”。模型回答、成功的计算结果或未找到反例，
> 都不会自动成为证明。只有确定性检查、fresh independent audit 和 canonical gate
> 可以改变可信状态。

[English README](README.md) · [快速开始](docs/quickstart.zh-CN.md) ·
[配置](docs/configuration.zh-CN.md) · [Provider](docs/providers.zh-CN.md) ·
[架构](docs/architecture.md) · [可信模型](docs/trust-model.md)

## 核心边界

- **反证优先**：先进行有界、精确、便宜的反例搜索和一致性检查。
- **独立审计**：Auditor 从不可变 task packet 和证据 bundle 重新检查候选，而不是
  直接相信 producer 的自述。
- **Append-only 证据**：事件、artifact、candidate、route 和人工 steering 可持续复核。
- **崩溃恢复**：campaign 由可独立封存的 epoch 组成，可恢复 pending work 和 audit lease。
- **机械子工隔离**：one-shot 子工只执行有限、机械可验收任务，不能递归调用、选择研究
  方向或修改 canonical 状态。
- **Canonical gate**：未审计模型输出不能更新可信 claim、proof 或项目状态。
- **表示兼容检查**：显式记录 branch、localization、saturation、normalization、content、
  exceptional factors 和 combination scope，禁止未经审计的跨表示组合。
- **统一协议预检**：mock 与真实 App Server 路径使用同一 Structured Outputs gate。
- **Controller 管理 continuation**：困难任务可在同一 thread 内进行有界多 turn；单个
  turn 结束或模型自报 `PROOF` 不等于逻辑任务结束；首次 `BLOCKED` 必须先修复一次，
  turn/token 边界会把任务检查点续接到下一 epoch；provider quota 耗尽只暂停并保留任务，
  不计入数学失败或 stagnation。
- **Canonical proof frontier**：稳定 proof obligation 直接保存在 `ClaimGraph` 中，
  只有通过审计的 canonical 进展才能闭合 obligation 或重置 stagnation。

## 安装

需要 Python 3.11 或更高版本：

```console
python -m pip install autonomous-math-ai
```

发行名是 `autonomous-math-ai`，Python namespace 保持
`autonomous_math_research`，命令行入口保持 `amr`。

## 仓库边界

本仓库只保存通用 harness、中性模板、policy 资源与测试。数学命题、项目
prompt、claim/task graph、实验、审计、run、outcome 和 artifact 必须保存在
由 `--project` 指定的独立研究仓库中。`.agents/skills/math-research/` 只是
Codex 发现入口，引用同一份 package policy，不包含第二套 engine 或任何具体
研究状态。

## 零额度开始

```console
amr init ./research-target --project-id research-target --final-claim-id C_ROOT
amr validate --project ./research-target
amr config validate --project ./research-target
amr config explain --project ./research-target
amr run --project ./research-target --dry-run
amr run --project ./research-target --mock --hours 0.01
amr detect-tools --project-root ./research-target
```

这些命令不会启动真实模型。初始化骨架故意保留数学占位标记；补全命题和检查清单后，
再运行 `amr validate --strict`。Codex App Server 是开箱即用的默认 provider，复用
operator 的 Codex 登录；API 只是显式可选入口，不配置 API 就不会要求 API key。

## 长期运行结构

```text
campaign → epoch → job
```

epoch 到期、预算耗尽、operator stop 或内部失败后，controller 停止派发新任务并等待
健康在途任务自然结束，再封存状态。内部错误不会被伪装成“队列耗尽”。

Director 会收到由 controller 生成的 claim 表示兼容性与已审计 bridge 摘要。若本轮全部
任务均未通过语义准入，route 记录仍会保留，但 controller 只进行一次有界修复重规划；若
第二次仍无可执行 research/audit 工作，则干净暂停 campaign。单独的 route update 不会让
空执行队列继续运行。

## 机械子工

研究和审计角色只能通过 controller 管理的 broker 请求有限机械任务。默认使用
Spark/high/null；模型不可用、额度、传输或超时这四类 provider 执行失败允许一次
Luna/medium/null fallback。策略、权限、任务资格、schema、protocol 和 artifact 错误仍是
终止错误。临时错误不会缓存为模型不可用，也不会回退到父模型、fast 或 priority service。
默认没有静态子工席位上限，但仍受独立的 15 亿 token 默认额度、
CPU/系统资源、provider rate limit、256 深度队列、dispatch batch、超时和 operator stop
约束。主角色默认额度为 5 亿 token；监视面板并列显示两套额度，并在机械 telemetry 不完整
时明确显示观测下界和未知次数。

机械结果只是父角色可以检查的执行证据。父研究模型负责解释，强 Auditor 负责最终判词。

## 安全说明

- 不把认证秘密读取或保存为研究 artifact。
- 资产导入使用内容寻址和可移植 URI，并拒绝符号链接逃逸。
- 人工 steering 是 append-only 输入，不能直接设置 trust 或绕过审计。
- telemetry 缺失时记录为 unknown，而不是确定的 0。
- dry-run、mock、active live、failed live 和 completed live 明确区分。

项目目前处于 alpha 阶段。真实 campaign 前请阅读[快速开始](docs/quickstart.zh-CN.md)、
[配置文档](docs/configuration.zh-CN.md)、[Provider 文档](docs/providers.zh-CN.md)、
[架构](docs/architecture.md)、[可信模型](docs/trust-model.md)和
[安全策略](SECURITY.md)。

### Windows 单文件启动器

双击 [`amr-launcher.cmd`](amr-launcher.cmd)，或在安装后运行 `amr launcher`。这个通用
入口不保存项目名称、项目路径或研究配置。首次使用时输入工作区根目录；入口只把该选择
保存到 `%LOCALAPPDATA%\autonomous-math-ai\launcher.json`，以后每次重新扫描 Git 可见的
`autonomous/project.json`。

选择项目后可执行 validate、strict、脱敏配置查看、dry-run、mock，以及需要输入
`RUN <project_id>` 的真实运行。持久设置只放在 manifest 指定的
`autonomous/config.yaml`；菜单修改是经过完整预检的一次性临时覆盖，命令结束后删除。
dry-run、mock 或 real 启动时还会同步打开精确绑定本次 run 的独立监视窗口。密钥只能
使用凭据引用，不能写入入口或项目配置。

每次新 run 会在首轮 Director 前按 `autonomous/project.json` 冻结 canonical inputs 的
路径、SHA-256 与可用 Git revision，并重建 run-local snapshot、`CORE_CAPSULE` 和
`RESEARCH_MAP`。动态 canonical 内容覆盖陈旧的项目 Director/frontier 描述；不能安全
同步的漂移会在模型 turn 前 fail closed，刷新过程不改写 canonical 项目文件。

本项目采用 [MIT License](LICENSE)。
