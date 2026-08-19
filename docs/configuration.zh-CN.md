# 配置与 profile

配置 schema v8 的合并顺序是：内置 `codex-app-server-default`、项目
`autonomous/config.json`、显式 `--profile`。最后执行核心可信边界校验，因此项目或
用户 profile 都不能移除核心 protected paths、关闭 final claim 独立审计、替换持久
controller、开放机械子工网络或启用 fast/priority/auto tier。

以下命令只读、自动脱敏、不会启动模型：

```console
amr config validate --project ./research-target
amr config explain --project ./research-target --profile ./my-profile.json
```

每个角色可独立声明 provider、model、endpoint/profile、effort 及不支持时的显式
策略、service tier、输出规范化、timeout、两类 retry、concurrency、token limit、
cost limit 和 estimated cost。默认仍全部走 Codex App Server；API 只在显式路由后
使用。

新项目主角色默认额度为 5 亿 token；机械子工独立额度为 15 亿。机械席位上限默认
`null`，只表示没有静态数量上限，broker 仍受预算、系统资源、rate limit、队列、
dispatch batch、超时和 operator stop 约束。策略可选 `preferred`、`balanced`、
`conservative`、`disabled` 或带阈值的 `custom`。

用户 profile 必须只含 `profile_schema_version`、`name`、`extends`、`overrides`，且
不能改项目 ID。v7 项目配置会在内存中迁移到 v8，不会改写源文件。示例见
[`examples/per-role-api-profile.json`](examples/per-role-api-profile.json)。
