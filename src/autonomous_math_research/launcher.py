from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

from .config import HarnessConfig, deep_merge, load_config
from .profiles import BUILTIN_PROFILE_NAME, PROFILE_SCHEMA_VERSION, load_user_profile
from .project import ProjectManifest
from .provider_config import redact_config


LAUNCHER_STATE_SCHEMA_VERSION = 1
EXCLUDED_SCAN_DIRS = frozenset({
    ".git", ".validation", ".venv", "venv", "__pycache__", "_runtime",
    "build", "dist", "runs", "outcomes", "worktrees", "node_modules",
})
ROLE_ORDER = (
    "director", "prover", "falsifier", "explorer", "auditor",
    "evaluator_auditor", "smoke",
)
ROLE_NAMES = frozenset(ROLE_ORDER)
COMMON_OVERRIDE_PATHS = (
    ("campaign.hours", "campaign 总时长（小时）"),
    ("campaign.epoch_hours", "单个 epoch 最长时间（小时）"),
    ("scheduler.max_research_workers", "research 并发"),
    ("scheduler.max_audit", "audit 并发"),
    ("scheduler.max_mechanical_subworkers", "机械子工静态上限，null 表示无静态上限"),
    ("budgets.global_tokens", "主角色 token 预算"),
    ("budgets.mechanical_tokens", "机械子工 token 预算"),
    ("budgets.global_cost_usd", "主角色 cost 上限，null 表示未设置"),
    ("budgets.mechanical_cost_usd", "机械子工 cost 上限，null 表示未设置"),
) + tuple(
    (f"models.{role}.{field}", f"{role} {label}")
    for role in ROLE_ORDER
    for field, label in (
        ("provider", "provider"),
        ("model", "model"),
        ("effort", "effort"),
        ("service_tier", "service tier"),
        ("timeout_seconds", "timeout（秒）"),
        ("retries.transport", "transport retry"),
        ("retries.model_protocol", "protocol retry"),
        ("token_limit", "token 上限"),
        ("cost_limit_usd", "cost 上限"),
    )
) + tuple(
    (f"policy.one_shot_compute_worker.{route}.{field}", f"机械 {label}")
    for route, route_label in (
        ("primary_route", "primary"),
        ("fallback_route", "fallback"),
    )
    for field, field_label in (
        ("provider", "provider"),
        ("model", "model"),
        ("effort", "effort"),
        ("service_tier", "service tier"),
    )
    for label in (f"{route_label} {field_label}",)
) + (
    (
        "policy.one_shot_compute_worker.selection_policy.mode",
        "机械 selection policy",
    ),
)

_SIMPLE_OVERRIDE_PATTERNS = (
    re.compile(r"^campaign\.(?:hours|epoch_hours)$"),
    re.compile(
        r"^scheduler\.(?:max_research_workers|max_audit|max_mechanical_subworkers)$"
    ),
    re.compile(
        r"^budgets\.(?:global_tokens|mechanical_tokens|global_cost_usd|mechanical_cost_usd)$"
    ),
    re.compile(
        r"^engine\.(?:max_retries|transient_protocol_max_retries|"
        r"model_protocol_max_retries|director_max_retries)$"
    ),
    re.compile(
        r"^models\.([a-z_]+)\.(?:provider|model|endpoint|profile|effort|"
        r"unsupported_effort|service_tier|output_mode|timeout_seconds|"
        r"max_concurrency|token_limit|cost_limit_usd|estimated_cost_usd)$"
    ),
    re.compile(
        r"^models\.([a-z_]+)\.retries\.(?:transport|model_protocol)$"
    ),
    re.compile(
        r"^policy\.one_shot_compute_worker\.selection_policy\.mode$"
    ),
    re.compile(
        r"^policy\.one_shot_compute_worker\.(?:primary_route|fallback_route)\."
        r"(?:provider|model|endpoint|profile|effort|unsupported_effort|service_tier)$"
    ),
)


@dataclass(frozen=True, slots=True)
class LauncherProject:
    project_id: str
    final_claim_id: str
    root: Path
    config_path: Path


def default_launcher_state_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "autonomous-math-ai" / "launcher.json"
    return Path.home() / ".config" / "autonomous-math-ai" / "launcher.json"


def load_launcher_state(path: Path | None = None) -> dict[str, Any]:
    source = (path or default_launcher_state_path()).resolve()
    if not source.is_file():
        return {
            "schema_version": LAUNCHER_STATE_SCHEMA_VERSION,
            "workspace_root": None,
            "last_project_id": None,
        }
    raw = json.loads(source.read_text(encoding="utf-8"))
    required = {"schema_version", "workspace_root", "last_project_id"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("launcher state fields are invalid")
    if raw["schema_version"] != LAUNCHER_STATE_SCHEMA_VERSION:
        raise ValueError("unsupported launcher state schema version")
    for key in ("workspace_root", "last_project_id"):
        if raw[key] is not None and not isinstance(raw[key], str):
            raise ValueError(f"launcher state {key} must be a string or null")
    return raw


def save_launcher_state(
    workspace_root: Path,
    last_project_id: str | None,
    path: Path | None = None,
) -> Path:
    target = (path or default_launcher_state_path()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": LAUNCHER_STATE_SCHEMA_VERSION,
        "workspace_root": str(workspace_root.resolve()),
        "last_project_id": last_project_id,
    }
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def _git_manifest_paths(workspace_root: Path) -> list[Path] | None:
    try:
        completed = subprocess.run(
            [
                "git", "-C", str(workspace_root), "ls-files", "-z",
                "--cached", "--others", "--exclude-standard",
            ],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    paths: list[Path] = []
    for item in completed.stdout.split(b"\0"):
        if not item:
            continue
        relative = Path(os.fsdecode(item))
        parts = tuple(part.lower() for part in relative.parts)
        if len(parts) >= 2 and parts[-2:] == ("autonomous", "project.json"):
            paths.append((workspace_root / relative).resolve())
    return paths


def _fallback_manifest_paths(workspace_root: Path, max_depth: int = 6) -> list[Path]:
    root = workspace_root.resolve()
    result: list[Path] = []
    for current_text, directories, files in os.walk(root):
        current = Path(current_text)
        depth = len(current.relative_to(root).parts)
        if current != root and (current / ".git").exists():
            directories[:] = []
            continue
        directories[:] = [
            name for name in directories
            if name.lower() not in EXCLUDED_SCAN_DIRS and depth < max_depth
        ]
        if current.name.lower() == "autonomous" and "project.json" in files:
            result.append((current / "project.json").resolve())
            directories[:] = []
    return result


def scan_workspace(workspace_root: Path) -> tuple[list[LauncherProject], list[str]]:
    root = workspace_root.resolve()
    if not root.is_dir():
        raise ValueError(f"workspace root is not a directory: {root}")
    manifest_paths = _git_manifest_paths(root)
    if manifest_paths is None:
        manifest_paths = _fallback_manifest_paths(root)
    projects: list[LauncherProject] = []
    issues: list[str] = []
    for manifest_path in sorted(set(manifest_paths)):
        project_root = manifest_path.parent.parent.resolve()
        try:
            manifest = ProjectManifest.load(project_root, manifest_path)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            issues.append(f"{manifest_path}: {exc}")
            continue
        projects.append(LauncherProject(
            project_id=manifest.project_id,
            final_claim_id=manifest.final_claim_id,
            root=project_root,
            config_path=manifest.resolve(manifest.config, must_exist=True),
        ))
    counts: dict[str, int] = {}
    for project in projects:
        counts[project.project_id] = counts.get(project.project_id, 0) + 1
    duplicates = {key for key, count in counts.items() if count > 1}
    if duplicates:
        for project_id in sorted(duplicates):
            matches = [str(item.root) for item in projects if item.project_id == project_id]
            issues.append(f"duplicate project_id {project_id!r}: {matches}")
        projects = [item for item in projects if item.project_id not in duplicates]
    return sorted(projects, key=lambda item: (item.project_id, str(item.root))), issues


def _dotted_value(raw: dict[str, Any], dotted_path: str) -> Any:
    value: Any = raw
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _set_dotted(raw: dict[str, Any], dotted_path: str, value: Any) -> None:
    target = raw
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        child = target.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"override path crosses a non-object: {dotted_path}")
        target = child
    target[parts[-1]] = value


def override_path_allowed(dotted_path: str) -> bool:
    for pattern in _SIMPLE_OVERRIDE_PATTERNS:
        match = pattern.fullmatch(dotted_path)
        if not match:
            continue
        if match.groups() and match.group(1) not in ROLE_NAMES:
            return False
        return True
    return False


def parse_override_assignment(text: str) -> tuple[str, Any]:
    if "=" not in text:
        raise ValueError("override must use dotted.path=JSON-value")
    dotted_path, raw_value = text.split("=", 1)
    dotted_path = dotted_path.strip()
    if not override_path_allowed(dotted_path):
        raise ValueError(
            "this field is not eligible for a one-time terminal override; "
            "edit the project config instead"
        )
    try:
        value = json.loads(raw_value.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(
            "override values use JSON syntax; quote strings, for example "
            'models.prover.effort="xhigh"'
        ) from exc
    return dotted_path, value


@contextmanager
def temporary_profile(
    overrides: dict[str, Any],
    base_profile_path: Path | None = None,
) -> Iterator[Path | None]:
    if not overrides:
        yield base_profile_path.resolve() if base_profile_path is not None else None
        return
    base_overrides: dict[str, Any] = {}
    profile_name = "launcher-one-shot"
    if base_profile_path is not None:
        profile_name, base_overrides = load_user_profile(base_profile_path)
    combined = deep_merge(base_overrides, overrides)
    payload = {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "name": f"{profile_name}-launcher-one-shot",
        "extends": BUILTIN_PROFILE_NAME,
        "overrides": combined,
    }
    path = Path(tempfile.gettempdir()) / f"amr-launcher-{uuid4().hex}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def format_config_summary(config: HarnessConfig) -> str:
    summary = config.summarized()
    lines = [
        f"项目: {summary['project']}  final claim: {summary['final_claim_id']}",
        f"配置: {summary['project_config']}",
        (
            "时长: campaign={hours}h, epoch={epoch_hours}h"
        ).format(**summary["campaign"]),
        (
            "并发: research={max_research_workers}, audit={max_audit}, "
            "mechanical={max_mechanical_subworkers}"
        ).format(**summary["scheduler"]),
        (
            "预算: main={global_tokens}, mechanical={mechanical_tokens}, "
            "main_cost={global_cost_usd}, mechanical_cost={mechanical_cost_usd}"
        ).format(**summary["budgets"]),
        "角色路由:",
    ]
    for role, route in summary["roles"].items():
        lines.append(
            f"  {role}: {route['provider']}/{route['model']} effort={route['effort']} "
            f"timeout={route['timeout_seconds']} token={route['token_limit']}"
        )
    mechanical = summary["mechanical"]
    lines.extend([
        (
            "机械主路由: {provider}/{model} effort={effort} policy={policy}"
        ).format(
            **mechanical["primary_route"],
            policy=mechanical["selection_policy"]["mode"],
        ),
        (
            "机械回退: {provider}/{model} effort={effort}"
        ).format(**mechanical["fallback_route"]),
    ])
    if summary["migrations_applied"]:
        lines.append(f"内存迁移: {summary['migrations_applied']}")
    return "\n".join(lines)


def _run_amr(arguments: Sequence[str]) -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "autonomous_math_research", *arguments],
        check=False,
    )
    return int(completed.returncode)


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _monitor_command(project: Path, run_id: str) -> list[str]:
    return [
        sys.executable, "-m", "autonomous_math_research", "watch",
        "--project", str(project), "--run", run_id,
        "--wait-seconds", "60", "--chat",
    ]


def _open_monitor_window(
    project: Path, run_id: str, output: Callable[[str], None],
) -> bool:
    command = _monitor_command(project, run_id)
    try:
        if os.name == "nt":
            console_flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
            subprocess.Popen(
                [os.environ.get("COMSPEC", "cmd.exe"), "/k", *command],
                creationflags=console_flag,
            )
        else:
            output("当前平台未自动打开新终端；可另开终端执行: " + shlex.join(command))
            return False
    except OSError as exc:
        output(f"监视窗口启动失败（主运行仍将继续）: {exc}")
        return False
    output(f"监视窗口已启动，将跟随本次 run: {run_id}")
    return True


def _open_config_file(path: Path, output: Callable[[str], None]) -> None:
    editor = os.environ.get("EDITOR")
    try:
        if editor:
            subprocess.Popen([*shlex.split(editor), str(path)])
        elif os.name == "nt":
            subprocess.Popen(["notepad.exe", str(path)])
        else:
            output(f"请使用文本编辑器打开: {path}")
    except OSError as exc:
        output(f"无法启动编辑器: {exc}; 请手动打开 {path}")


def _edit_common_override(
    config: HarnessConfig,
    overrides: dict[str, Any],
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    output("可一次性修改的常用参数：")
    for index, (path, label) in enumerate(COMMON_OVERRIDE_PATHS, start=1):
        output(f"  {index}. {label}: {_dotted_value(config.raw, path)!r}  [{path}]")
    selected = input_fn("编号（留空取消）: ").strip()
    if not selected:
        return
    try:
        path = COMMON_OVERRIDE_PATHS[int(selected) - 1][0]
    except (ValueError, IndexError) as exc:
        raise ValueError("invalid common parameter number") from exc
    raw_value = input_fn("新值（JSON；字符串必须加双引号）: ")
    assignment_path, value = parse_override_assignment(f"{path}={raw_value}")
    _set_dotted(overrides, assignment_path, value)


def _prepare_and_run(
    project: LauncherProject,
    action: str,
    profile_path: Path | None,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    command_runner: Callable[[Sequence[str]], int],
) -> int:
    overrides: dict[str, Any] = {}
    while True:
        try:
            with temporary_profile(overrides, profile_path) as effective_profile:
                config = load_config(
                    project.root, require_manifest=True, profile_path=effective_profile,
                )
                output("\n" + format_config_summary(config))
                output(
                    "\n[Enter] 执行  [N] 常用参数  [O] dotted.path=JSON  "
                    "[V] 完整脱敏配置  [F] 编辑项目配置  [B] 返回"
                )
                choice = input_fn("选择: ").strip().lower()
                if choice == "":
                    if action == "real":
                        confirmation = input_fn(
                            f"输入 RUN {project.project_id} 启动真实模型: "
                        ).strip()
                        if confirmation != f"RUN {project.project_id}":
                            output("已取消真实运行。")
                            return 0
                    run_id = _new_run_id()
                    arguments = [
                        "run", "--project", str(project.root),
                        "--run-id", run_id,
                    ]
                    if effective_profile is not None:
                        arguments.extend(["--profile", str(effective_profile)])
                    if action == "dry-run":
                        arguments.append("--dry-run")
                    elif action == "mock":
                        arguments.append("--mock")
                    if command_runner is _run_amr:
                        _open_monitor_window(project.root, run_id, output)
                    return command_runner(arguments)
                if choice == "n":
                    _edit_common_override(config, overrides, input_fn, output)
                elif choice == "o":
                    output('示例: models.prover.effort="xhigh"')
                    output("示例: scheduler.max_mechanical_subworkers=null")
                    path, value = parse_override_assignment(
                        input_fn("一次性覆盖: ").strip()
                    )
                    _set_dotted(overrides, path, value)
                elif choice == "v":
                    output(json.dumps(
                        redact_config(config.raw), ensure_ascii=False, indent=2,
                    ))
                elif choice == "f":
                    _open_config_file(config.config_path, output)
                    input_fn("保存配置后按 Enter 重新校验: ")
                elif choice == "b":
                    return 0
                else:
                    output("未知选择。")
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            output(f"配置预检失败，未启动模型: {exc}")
            choice = input_fn(
                "输入 R 清除一次性覆盖，F 打开项目配置，B 返回: "
            ).strip().lower()
            if choice == "r":
                overrides.clear()
            if choice == "f":
                _open_config_file(project.config_path, output)
                input_fn("保存配置后按 Enter: ")
            if choice == "b":
                return 0


def _choose_project(
    projects: list[LauncherProject],
    last_project_id: str | None,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> LauncherProject | None:
    output("\n可用项目：")
    default_index: int | None = None
    for index, project in enumerate(projects, start=1):
        marker = " *" if project.project_id == last_project_id else ""
        if marker:
            default_index = index
        output(f"  {index}. {project.project_id}{marker}\n     {project.root}")
    prompt = "项目编号"
    if default_index is not None:
        prompt += f"（Enter={default_index}）"
    selected = input_fn(prompt + "，W=更换工作区，0=退出: ").strip().lower()
    if selected == "w":
        return None
    if selected == "0":
        raise EOFError
    if not selected and default_index is not None:
        selected = str(default_index)
    try:
        return projects[int(selected) - 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("invalid project selection") from exc


def run_launcher(
    *,
    workspace_root: Path | None = None,
    project_root: Path | None = None,
    action: str | None = None,
    profile_path: Path | None = None,
    state_path: Path | None = None,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
    command_runner: Callable[[Sequence[str]], int] = _run_amr,
) -> int:
    state = load_launcher_state(state_path)
    explicit_workspace = workspace_root or (
        Path(os.environ["AMR_WORKSPACE_ROOT"])
        if os.environ.get("AMR_WORKSPACE_ROOT") else None
    )
    explicit_project = project_root or (
        Path(os.environ["AMR_PROJECT_ROOT"])
        if os.environ.get("AMR_PROJECT_ROOT") else None
    )
    if explicit_project is not None:
        manifest = ProjectManifest.load(explicit_project.resolve())
        project = LauncherProject(
            manifest.project_id, manifest.final_claim_id, explicit_project.resolve(),
            manifest.resolve(manifest.config, must_exist=True),
        )
        selected_workspace = explicit_workspace or project.root
    else:
        selected_workspace = explicit_workspace or (
            Path(state["workspace_root"]) if state.get("workspace_root") else None
        )
        while True:
            if selected_workspace is None:
                raw = input_fn("首次使用，请输入工作区根目录: ").strip()
                if not raw:
                    output("未选择工作区。")
                    return 2
                selected_workspace = Path(raw)
            try:
                projects, issues = scan_workspace(selected_workspace)
            except (ValueError, OSError) as exc:
                output(f"工作区扫描失败: {exc}")
                selected_workspace = None
                continue
            for issue in issues:
                output(f"扫描警告: {issue}")
            if not projects:
                output("未发现有效的 autonomous/project.json。")
                selected_workspace = None
                continue
            try:
                project = _choose_project(
                    projects, state.get("last_project_id"), input_fn, output,
                )
            except EOFError:
                return 0
            except ValueError as exc:
                output(str(exc))
                continue
            if project is None:
                selected_workspace = None
                continue
            break
    save_launcher_state(selected_workspace, project.project_id, state_path)

    normalized_action = action.lower() if action else None
    direct_actions = {"validate", "strict", "config", "dry-run", "mock", "real"}
    if normalized_action is not None and normalized_action not in direct_actions:
        raise ValueError(f"unsupported launcher action: {action}")
    while True:
        if normalized_action is None:
            output(f"\n当前项目: {project.project_id}\n  {project.root}")
            output(
                "1.Validate  2.Strict  3.Config  4.Dry-run  5.Mock  "
                "6.Real  7.Switch project  0.Exit"
            )
            selected = input_fn("选择: ").strip()
            mapping = {
                "1": "validate", "2": "strict", "3": "config",
                "4": "dry-run", "5": "mock", "6": "real",
            }
            if selected == "0":
                return 0
            if selected == "7":
                return run_launcher(
                    workspace_root=selected_workspace, profile_path=profile_path,
                    state_path=state_path, input_fn=input_fn, output=output,
                    command_runner=command_runner,
                )
            selected_action = mapping.get(selected)
            if selected_action is None:
                output("未知选择。")
                continue
        else:
            selected_action = normalized_action

        if selected_action in {"dry-run", "mock", "real"}:
            result = _prepare_and_run(
                project, selected_action, profile_path, input_fn, output, command_runner,
            )
        elif selected_action == "config":
            arguments = ["config", "summary", "--project", str(project.root)]
            if profile_path is not None:
                arguments.extend(["--profile", str(profile_path)])
            result = command_runner(arguments)
        else:
            arguments = ["validate", "--project", str(project.root)]
            if selected_action == "strict":
                arguments.append("--strict")
            if profile_path is not None:
                arguments.extend(["--profile", str(profile_path)])
            result = command_runner(arguments)
        if normalized_action is not None:
            return result
        input_fn("按 Enter 返回菜单: ")
