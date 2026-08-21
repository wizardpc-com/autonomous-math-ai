# Provider adapter

Codex App Server 是默认 provider，复用 operator 已有的 Codex 登录，项目配置不需要
API key。内置 `openai_compatible` adapter 是显式可选入口，可适配 Responses 或
Chat Completions 风格 HTTP API。

provider transport 与数学角色协议解耦。无论模型来自哪里，都必须通过统一 schema
preflight、错误分类、token/cost telemetry 规范化、retry、canonical gate 和独立审计；
provider 返回值从不自动成为证明。

capability 必须声明 structured-output 模式、reasoning 参数和支持的 effort、显式 effort
mapping、安全 service tiers、总/cached/uncached/cache-write input、output、reasoning
output token 字段路径、可选 cost 路径及机械 one-shot 能力。不
支持的 effort 默认预检失败；只有 route 选择 `map` 且 capability 给出精确映射时才允许，
禁止静默降级。没有原生 Structured Outputs 的 provider 必须显式使用 `json_text`，本地
schema gate 仍然执行。

凭据只能写 `{kind, reference}`；kind 为 `environment`、`system_credential`、
`provider_profile` 或 `none`。环境变量方式只保存 `OPENAI_API_KEY` 这样的变量名。
validate/explain 不读取变量值，真实 adapter 发请求时才解析引用。

adapter 必须把 provider quota 耗尽与普通 rate limit 分开。usage-limit/quota-exhausted 在
当前 epoch 内不重试；controller 保存 provider 给出的 reset 时间，暂停 campaign，并保留
任务 frontier。该状态不构成数学失败或 stagnation。

第三方包可在 Python entry-point group `autonomous_math_research.providers` 注册 factory；
未安装但被角色引用的 adapter 会在零模型配置预检中失败。内置机械 runner 对应
`codex_app_server`。其他 adapter 若声明 `mechanical_one_shot: true`，还必须以同一 adapter
ID 在 `autonomous_math_research.mechanical_runners` 注册 controller-managed one-shot runner
factory；primary/fallback 必须共用一个 runner adapter。packet 校验、预算、背压、artifact
gate、重试/恢复记录和递归禁止仍由 controller 掌握，未安装时在零模型预检失败。
自定义 compatible gateway 示例见
[`examples/custom-provider-profile.json`](examples/custom-provider-profile.json)。
