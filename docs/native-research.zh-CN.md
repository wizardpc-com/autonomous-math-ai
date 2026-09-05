# Astra 与原生研究交接操作手册

两条执行路径共用现有证据和审计门禁：原生 Codex 用于开放探索，AMR 用于输入、停止条件和验收边界已明确的任务。原生研究者只能提交未审计成果；可信状态仍由 AMR 的 canonical transaction 更新。

## 选择 Astra，仅对新运行生效

保留项目配置，显式传入 [Astra profile](examples/astra-research-profile.json)：

```console
amr config explain --project ./target --profile ./docs/examples/astra-research-profile.json
amr model-probe --project ./target --profile ./docs/examples/astra-research-profile.json
```

第二条默认只检查本地配置与输出 schema，返回 `LIVE_NOT_RUN`，不启动 App Server 或模型。Astra 路线为 prover/auditor/evaluator_auditor `xhigh`，director/falsifier/explorer/smoke `high`；Fast 关闭。机械路线和原有预算保持有效项目配置的值。启动研究前用 `config summary` 检查预算及并发；该 profile 不代表小额 campaign 配置。短 reasoning 的 retry allowance 为零，候选结构修复、预算、工具错误和审计门禁照常生效。

仅在另行授权 live 后使用：

```console
amr model-probe --project ./target --profile ./docs/examples/astra-research-profile.json --live --timeout 90 --budget 4000
```

最多两次独立会话的短回合、无重试、90 秒本地工作期限，结果保存在项目 runtime 的 `compatibility_probes/`。token 限额阻止后续派发，进行中的回合可能超额；进程关闭另有有界清理时间。模型/effort 未观测到仍是 `UNKNOWN`；thread/start 的配置确认不代表逐回合实际路线。旧 Terra smoke 不能验证 Astra。这里没有运行 campaign。

模型名及接口依据：[Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)、[App Server model/list](https://learn.chatgpt.com/docs/app-server#models)。本机账户能力必须由 App Server 的分页结果验证；官方能力说明不能证明本机可用。

## 原生探索与回流

准备一个现有 `ResearchTask` JSON：精确命题、范围、与目标的关系、依赖 ClaimGraph IDs、必要文件、表示契约、停止条件。任务格式与 Director 的 `spawn` 项一致；可使用任务对象的 `to_dict()`，允许现有 `output_contract`。数学前提是否齐全仍需研究者确认，导出不等同于候选准入。

```console
amr frontier rebuild --project ./target
amr handoff export --project ./target --task ./task.json --output ./native-task --budget 20000 --profile ./docs/examples/astra-research-profile.json
```

`native-task` 必须位于项目树外且尚不存在。先保存导出返回的 `input_sha256` 到生产者工作区之外，再打开原生 Codex。读取 `NATIVE_README.md`、`task.json`、`context.json`、`binding.json` 和冻结的定义；共享 policy 由现有 pinning 程序复制并验证，未建立另一套 Skill 或研究引擎。写入 `output/`，填写 `result.template.json`，把证明、缺口、代码、实际命令、版本、输出日志和复现说明加入证据列表。占位哈希由程序重算。

文件选择支持项目相对路径和 `project://`；没有 campaign/epoch 隐式解析。需要补充输入或更换表示时，明确补充项目文件并重新导出，保存新的绑定。允许提出窄的新引理，但不能把它们当已验证依赖；依赖字段只接受现有 ClaimGraph claim IDs。

```console
amr handoff seal --workspace ./native-task --result ./native-task/result.template.json --output ./sealed-result --input-sha256 <retained-export-hash>
amr handoff import --project ./target --bundle ./sealed-result
amr frontier rebuild --project ./target
amr frontier context --project ./target --claim C_ROOT
```

封存目录必须是新目录。原生结果只能为 `UNAUDITED_EXTERNAL_RESULT` 或 `COMPUTATION_ONLY`；后者的 conclusion 必须是 `COMPUTATION`。导入拒绝生产者提供的审计权限、来源变化、表示/范围变化、路径逃逸和同 ID 的不同内容。冻结输入清单及其所有文件引用必须齐全，binding 与来源哈希必须一致；封存期间清单改写也会被拒绝。原始证据按内容哈希保留，不覆盖旧成果。

导入只建立外部结果清单；返回 `candidate_queue_entered=false`、`audit_receipt_created=false`、`canonical_authority_changed=false`。Frontier rebuild 才更新路由视图，仍不会产生可信 PASS。记录为待审计、进入候选队列、取得 canonical 晋升是三个独立步骤。

正式复核另开顶层会话和单独审计工作区，提供封存证明及冻结定义，不 fork 生产者会话，不传聊天记录或信心陈述。关键计算由审计者从定义另写实现；同脚本 replay 只算复现。将实际独立审核记录通过既有外部结果审核流程登记；需要 ClaimGraph 晋升时，由项目的候选 helper/审计队列或既有 `reconcile stage/inspect/apply` 路径处理，并继续接受语义、独立审计和 canonical 门禁。不要把未审计原生结果直接改名为已审计结果。

## 权限、回滚与比较

本入口报告 `SUPERVISED_PROCESS_SEPARATION`：独立目录及操作规约不构成 OS 隔离。开始原生研究前核对该会话实际 writable roots；只有 `output/` 和结果草稿应可写。原生会话不应拥有项目权威目录的写权限。当前环境无法限制时采用人工监督，保持一个 canonical writer。导入支持内容一致的重复操作；损坏或冲突留下的材料保留供检查，不自动清理旧证据。

回滚只需新运行移除 `--profile` 或传原有 profile，继续使用原 AMR 安装。旧 run 的 resume 仍使用原先的配置与 policy 快照；不要把已封存或禁止继续的 campaign 改名恢复。已导入证据保留，通过新的审核记录解释修正。

完成这层适配后冻结扩建。按 [小型对照计划](native-comparison.md) 记录实际收益，遇到可复现的重复摩擦再做小补丁。
