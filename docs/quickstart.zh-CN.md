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

## 恢复与继续

- `--resume` 只用于同一个尚未封存、因崩溃中断的 epoch。
- `amr campaign continue` 从最近的封存 checkpoint 创建新 epoch。
- 对已暂停并封存的 epoch，应先用独立的新 dry-run 验证升级后的 harness，再继续原
  campaign；不要对它使用 `--resume`，也不要把 dry-run 写入原真实 campaign。
- 历史失败 epoch 保持不可变，新 epoch 只导入仍未终止的 frontier。
