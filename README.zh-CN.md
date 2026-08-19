# Autonomous Math AI

**Autonomous Math AI — 可审计的数学研究 AI 编排工具**

Autonomous Math AI 是一个与具体猜想解耦的 Python harness，用于组织长期运行的
AI 辅助数学研究。它负责研究任务、反证搜索、独立审计、持久化证据、崩溃恢复和
受控机械子工，但不会把模型输出自动当作数学事实。

> Autonomous Math AI 不是“证明预言机”。模型回答、成功的计算结果或未找到反例，
> 都不会自动成为证明。只有确定性检查、fresh independent audit 和 canonical gate
> 可以改变可信状态。

[English README](README.md) · [快速开始](docs/quickstart.md) ·
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

## 安装

需要 Python 3.11 或更高版本：

```console
python -m pip install autonomous-math-ai
```

发行名是 `autonomous-math-ai`，Python namespace 保持
`autonomous_math_research`，命令行入口保持 `amr`。

## 零额度开始

```console
amr init ./research-target
amr validate --project ./research-target
amr run --project ./research-target --dry-run
amr run --project ./research-target --mock --hours 0.01
```

这些命令不会启动真实模型。真实运行需要单独配置 Codex App Server，并应在确认模型、
预算、权限、canonical 输入和审计规则后显式启动。

## 长期运行结构

```text
campaign → epoch → job
```

epoch 到期、预算耗尽、operator stop 或内部失败后，controller 停止派发新任务并等待
健康在途任务自然结束，再封存状态。内部错误不会被伪装成“队列耗尽”。

## 机械子工

研究和审计角色只能通过 controller 管理的 broker 请求有限机械任务。默认使用
Spark/high/null；只有明确的永久 unavailable/access denied 才允许一次
Luna/medium/null fallback。临时错误不会缓存为模型不可用，也不会回退到父模型、fast
或 priority service。

机械结果只是父角色可以检查的执行证据。父研究模型负责解释，强 Auditor 负责最终判词。

## 安全说明

- 不把认证秘密读取或保存为研究 artifact。
- 资产导入使用内容寻址和可移植 URI，并拒绝符号链接逃逸。
- 人工 steering 是 append-only 输入，不能直接设置 trust 或绕过审计。
- telemetry 缺失时记录为 unknown，而不是确定的 0。
- dry-run、mock、active live、failed live 和 completed live 明确区分。

项目目前处于 alpha 阶段。真实 campaign 前请阅读[快速开始](docs/quickstart.md)、
[架构](docs/architecture.md)、[可信模型](docs/trust-model.md)和
[安全策略](SECURITY.md)。

本项目采用 [MIT License](LICENSE)。
