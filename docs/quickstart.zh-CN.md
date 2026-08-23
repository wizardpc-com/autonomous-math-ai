# 快速开始

以下流程全部是零模型检查，不需要 API key。

```console
python -m pip install autonomous-math-ai
amr init ./research-target --project-id research-target --final-claim-id C_ROOT
amr validate --project ./research-target
amr config validate --project ./research-target
amr config explain --project ./research-target
amr run --project ./research-target --dry-run
```

以上 `amr init` 默认选择向后兼容的 `math-research`。另外两个内置 Phase 1 域可用：

```console
amr init ./checker-target --domain certified-computational-research
amr init ./study-target --domain empirical-research
```

发行名 `autonomous-math-ai`、Python namespace `autonomous_math_research` 和 CLI `amr`
均不改变。详见[研究域与 policy pack](research-domains.md)。

## 可选：Windows 双击入口

源码仓库根目录提供 [`../amr-launcher.cmd`](../amr-launcher.cmd)，安装后也可直接运行
`amr launcher`。入口是通用 bootstrap，不要写入项目路径或研究配置。首次使用时输入
工作区根目录；该选择只保存在用户 LOCALAPPDATA 中，以后每次启动重新扫描 Git 可见的
`autonomous/project.json`。

项目 manifest 指向唯一持久配置 `autonomous/config.yaml`。dry-run、mock、real 前会
展示脱敏摘要；常用编号修改或 `dotted.path=JSON值` 高级输入只生成临时 profile，绝不
写回项目。真实运行必须准确输入 `RUN <project_id>`。每种 run 操作还会同步打开一个精确
绑定新 run ID 的独立监视窗口。

`amr init` 会创建 README、AGENTS、初始化检查清单、claims、state、proofs、
tasks、experiments、certificates、audit、sources、conversations、artifacts 和
autonomous adapter。骨架中的 `AMR_PLACEHOLDER` 是有意保留的；准确填写命题、
定义域、量词、假设和依赖，并同步 claim graph 后再运行：

```console
amr validate --project ./research-target --strict
```

Codex App Server 是默认 provider，复用现有 Codex 登录。API 是显式可选入口；只有
role/profile 选择 API provider 时才会使用。配置中只能写环境变量名、系统凭据名或
provider profile 名，不能写密钥值。

确定性 mock 生命周期同样不会启动真实模型：

```console
amr run --project ./research-target --mock --hours 0.01
amr status --project ./research-target --run latest
```

真实运行前请检查每个角色的 provider/model/effort/tier/timeout/retry/concurrency/
token/cost，确认 canonical inputs、protected paths、独立审计和机械子工背压。省略
`--mock` 与 `--dry-run` 才会进入真实 provider。

一次启动自动跨 epoch 运行到 campaign 总时长耗尽可使用：

```console
amr run --project ./research-target --hours 12 --epoch-hours 2 --auto-epochs
```

它不会拉长单个 epoch；每次仍独立封存并重新执行 canonical refresh。quota pause、状态
冲突/同步失败、内部失败、人工停止或数学完成时不会启动下一 epoch。

## 恢复与继续

- `--resume` 只用于同一个尚未封存、因崩溃中断的 epoch。
- `amr campaign continue` 从最近的封存 checkpoint 创建新 epoch。
- `amr campaign continue --auto-epochs` 从该 checkpoint 启动，并在后续干净边界继续
  无人值守跨 epoch。
- `--auto-epochs` 在同一次命令中重复这一 fresh-epoch 边界，保留原 checkpoint/seal 语义。
- 崩溃恢复后继续无人值守可用
  `amr run --project PATH --resume EPOCH_ID --auto-epochs`；只有原 epoch 安全恢复并封存后
  才会启动下一 epoch。
- 若 `campaign continue` 发现未封存 epoch，会直接给出对应的 `--resume` 命令。
- 对已暂停并封存的 epoch，应先用独立的新 dry-run 验证升级后的 harness，再继续原
  campaign；不要对它使用 `--resume`，也不要把 dry-run 写入原真实 campaign。
- 历史失败 epoch 保持不可变，新 epoch 只导入仍未终止的 frontier。
- epoch 封存后，监视窗口会保持“成果归档中”，直到 report、索引、OUTCOME 和 run
  summary 全部持久化才退出。Ctrl+C 返回结构化 130；交互窗口仍默认在内部失败时停留，
  可用 `amr watch --no-hold-on-error ...` 关闭该行为。
