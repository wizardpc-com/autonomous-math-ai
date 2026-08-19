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
