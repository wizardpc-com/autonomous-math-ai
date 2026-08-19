#!/usr/bin/env python3
"""Quick, read-only inventory of mathematical command-line tools."""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


REPO_ROOT = Path(
    os.environ.get("MATH_WORKER_REPOSITORY_ROOT", str(Path.cwd()))
).resolve()
WSL_DISTRO = "Ubuntu-22.04"
WSL_USER_TOOL_ROOT = "~/.local/math-tools/bin"


@dataclass(frozen=True)
class Probe:
    name: str
    candidates: tuple[str, ...]
    version_args: tuple[str, ...]
    accept: Callable[[str], bool] = lambda _text: True
    extra_paths: tuple[str, ...] = ()


def expand_paths(patterns: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in patterns:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if any(char in expanded for char in "*?["):
            paths.extend(Path(match) for match in sorted(glob.glob(expanded), reverse=True))
        else:
            paths.append(Path(expanded))
    return paths


def locate(probe: Probe) -> Path | None:
    for candidate in probe.candidates:
        found = shutil.which(candidate)
        if found:
            return Path(found).resolve()
    for path in expand_paths(probe.extra_paths):
        if path.is_file():
            return path.resolve()
    return None


def run_version(executable: Path, args: tuple[str, ...]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [str(executable), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"probe failed: {exc}"
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    text = " | ".join(lines[:3])[:500]
    # Several solvers and MSVC print help/version text with a nonzero exit code.
    available = bool(text) and completed.returncode not in {126, 127, 9009}
    return available, text or f"exited with code {completed.returncode} without version output"


def command_text(executable: Path, args: tuple[str, ...]) -> str:
    parts = [str(executable), *args]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def inventory_probe(probe: Probe) -> dict[str, object]:
    executable = locate(probe)
    if executable is None:
        return {
            "name": probe.name,
            "available": False,
            "executable": None,
            "path": None,
            "version": None,
            "invocation": None,
        }
    launched, version = run_version(executable, probe.version_args)
    available = launched and probe.accept(version)
    return {
        "name": probe.name,
        "available": available,
        "executable": executable.name,
        "path": str(executable),
        "version": version,
        "invocation": command_text(executable, probe.version_args),
    }


def detect_wsl_sage_tool(
    name: str, binary: str, version_args: tuple[str, ...]
) -> dict[str, object] | None:
    """Probe a tool directly in the WSL user's isolated Sage environment."""
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if not wsl:
        return None
    argument_text = shlex.join(version_args)
    executable_hint = f"~/.local/miniforge3/envs/sage/bin/{binary}"
    shell_command = (
        f"test -x {executable_hint} || exit 127; "
        f'printf "%s\\n" {executable_hint}; '
        f"exec {executable_hint} {argument_text} 2>&1"
    )
    try:
        completed = subprocess.run(
            [wsl, "-d", WSL_DISTRO, "--", "sh", "-lc", shell_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) < 2:
        return None
    executable, *version_lines = lines
    version = " | ".join(version_lines[:3])[:500]
    invocation = subprocess.list2cmdline(
        [wsl, "-d", WSL_DISTRO, "--", executable, *version_args]
    )
    return {
        "name": name,
        "available": True,
        "executable": binary,
        "path": f"WSL:{WSL_DISTRO}:{executable}",
        "version": version,
        "invocation": invocation,
    }


def detect_wsl_user_tool(
    name: str, binary: str, version_args: tuple[str, ...]
) -> dict[str, object] | None:
    """Probe a pinned user-level tool installed inside the configured WSL distro."""
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if not wsl:
        return None
    argument_text = shlex.join(version_args)
    executable_hint = f"{WSL_USER_TOOL_ROOT}/{binary}"
    shell_command = (
        f"test -x {executable_hint} || exit 127; "
        f'printf "%s\\n" {executable_hint}; '
        f"exec {executable_hint} {argument_text} 2>&1"
    )
    try:
        completed = subprocess.run(
            [wsl, "-d", WSL_DISTRO, "--", "sh", "-lc", shell_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) < 2:
        return None
    executable, *version_lines = lines
    version = " | ".join(version_lines[:3])[:500]
    replay = f"{shlex.quote(executable)} {argument_text}"
    invocation = subprocess.list2cmdline(
        [wsl, "-d", WSL_DISTRO, "--", "sh", "-lc", replay]
    )
    return {
        "name": name,
        "available": True,
        "executable": binary,
        "path": f"WSL:{WSL_DISTRO}:{executable}",
        "version": version,
        "invocation": invocation,
    }


def detect_wsl_path_tool(
    name: str, binary: str, version_args: tuple[str, ...]
) -> dict[str, object] | None:
    """Probe an executable available on PATH inside the configured WSL distro."""
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if not wsl:
        return None
    argument_text = shlex.join(version_args)
    quoted_binary = shlex.quote(binary)
    shell_command = (
        f"executable=$(command -v {quoted_binary}) || exit 127; "
        'printf "%s\\n" "$executable"; '
        f"exec \"$executable\" {argument_text} 2>&1"
    )
    try:
        completed = subprocess.run(
            [wsl, "-d", WSL_DISTRO, "--", "sh", "-lc", shell_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) < 2:
        return None
    executable, *version_lines = lines
    version = " | ".join(version_lines[:3])[:500]
    replay = f"{shlex.quote(executable)} {argument_text}"
    invocation = subprocess.list2cmdline(
        [wsl, "-d", WSL_DISTRO, "--", "sh", "-lc", replay]
    )
    return {
        "name": name,
        "available": True,
        "executable": binary,
        "path": f"WSL:{WSL_DISTRO}:{executable}",
        "version": version,
        "invocation": invocation,
    }


def detect_python_module(
    python: dict[str, object], name: str, module: str
) -> dict[str, object]:
    if not python["available"] or not python["path"]:
        return {
            "name": name,
            "available": False,
            "executable": None,
            "path": None,
            "version": None,
            "invocation": None,
        }
    executable = Path(str(python["path"]))
    code = f"import {module}; print({module}.__version__)"
    try:
        completed = subprocess.run(
            [str(executable), "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    launched = completed is not None and completed.returncode == 0
    version = completed.stdout.strip()[:500] if launched else None
    return {
        "name": name,
        "available": launched,
        "executable": executable.name,
        "path": str(executable),
        "version": version,
        "invocation": command_text(executable, ("-c", code)),
    }


def physical_memory_bytes() -> tuple[int | None, int | None]:
    """Return host total and currently available memory using stdlib only."""
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
        except (AttributeError, OSError, ValueError):
            return None, None
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = pages * page_size
        available = None
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                    break
        return total, available
    except (AttributeError, OSError, TypeError, ValueError):
        return None, None


def detect_nvidia_gpus() -> dict[str, object]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False, "path": None, "devices": [], "error": None}
    query = (
        "index,name,driver_version,memory.total,memory.free,compute_cap"
    )
    try:
        completed = subprocess.run(
            [
                executable,
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "path": executable,
            "devices": [],
            "error": str(exc),
        }
    if completed.returncode != 0:
        return {
            "available": False,
            "path": executable,
            "devices": [],
            "error": completed.stderr.strip()[:500],
        }
    devices = []
    for row in csv.reader(completed.stdout.splitlines()):
        if len(row) != 6:
            continue
        index, name, driver, total_mib, free_mib, compute_capability = (
            value.strip() for value in row
        )
        try:
            total_value = int(total_mib)
            free_value = int(free_mib)
        except ValueError:
            continue
        devices.append(
            {
                "index": int(index),
                "name": name,
                "driver_version": driver,
                "memory_total_mib": total_value,
                "memory_free_mib": free_value,
                "compute_capability": compute_capability,
            }
        )
    return {
        "available": bool(devices),
        "path": executable,
        "devices": devices,
        "error": None,
    }


ACCELERATION_MODULES = ("numpy", "scipy", "numba", "cupy", "torch", "jax", "joblib")


def module_inventory_code() -> str:
    return (
        "import importlib.util,json\n"
        f"names={list(ACCELERATION_MODULES)!r}\n"
        "print(json.dumps({name: bool(importlib.util.find_spec(name)) for name in names}))\n"
    )


def detect_python_acceleration(executable: str | Path) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [str(executable), "-c", module_inventory_code()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "probe_succeeded": False,
            "available": False,
            "modules": {},
            "error": str(exc),
        }
    try:
        modules = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except json.JSONDecodeError:
        modules = {}
    return {
        "probe_succeeded": bool(modules),
        "available": any(bool(value) for value in modules.values()),
        "modules": modules,
        "error": None if modules else completed.stderr.strip()[:500],
    }


def detect_wsl_resources() -> dict[str, object]:
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if not wsl:
        return {"available": False, "distro": WSL_DISTRO, "error": "wsl not found"}
    shell_command = (
        "nproc; "
        "grep '^MemTotal:' /proc/meminfo; "
        "grep '^MemAvailable:' /proc/meminfo; "
        "if test -e /dev/dxg; then echo 1; else echo 0; fi"
    )
    try:
        completed = subprocess.run(
            [wsl, "-d", WSL_DISTRO, "--", "sh", "-lc", shell_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "distro": WSL_DISTRO, "error": str(exc)}
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) != 4:
        return {
            "available": False,
            "distro": WSL_DISTRO,
            "error": completed.stderr.strip()[:500] or "unexpected resource output",
        }
    try:
        logical_cpus = int(lines[0])
        total_kib = int(lines[1].split()[1])
        available_kib = int(lines[2].split()[1])
        dxg = int(lines[3])
    except ValueError:
        return {"available": False, "distro": WSL_DISTRO, "error": "invalid resource output"}

    sage_python = "~/.local/miniforge3/envs/sage/bin/python"
    acceleration_command = (
        f"test -x {sage_python} || exit 127; "
        f"exec {sage_python} -c {shlex.quote(module_inventory_code())}"
    )
    acceleration = {
        "probe_succeeded": False,
        "available": False,
        "modules": {},
        "error": "not probed",
    }
    try:
        modules_run = subprocess.run(
            [wsl, "-d", WSL_DISTRO, "--", "sh", "-lc", acceleration_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
        modules = json.loads(modules_run.stdout) if modules_run.returncode == 0 else {}
        acceleration = {
            "probe_succeeded": bool(modules),
            "available": any(bool(value) for value in modules.values()),
            "modules": modules,
            "error": None if modules else modules_run.stderr.strip()[:500],
        }
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        acceleration = {
            "probe_succeeded": False,
            "available": False,
            "modules": {},
            "error": str(exc),
        }
    return {
        "available": True,
        "distro": WSL_DISTRO,
        "logical_cpus": logical_cpus,
        "memory_total_bytes": total_kib * 1024,
        "memory_available_bytes": available_kib * 1024,
        "gpu_bridge_present": bool(dxg),
        "sage_python_acceleration": acceleration,
        "error": None,
    }


def detect_mathlib(lake: dict[str, object], lean: dict[str, object]) -> dict[str, object]:
    config_paths = [REPO_ROOT / "lakefile.toml", REPO_ROOT / "lakefile.lean"]
    configs = [path for path in config_paths if path.is_file()]
    mentions_mathlib = False
    for path in configs:
        try:
            mentions_mathlib = mentions_mathlib or "mathlib" in path.read_text(
                encoding="utf-8", errors="replace"
            ).lower()
        except OSError:
            pass
    package_present = (REPO_ROOT / ".lake" / "packages" / "mathlib").is_dir()
    ready = bool(lake["available"] and lean["available"] and mentions_mathlib and package_present)
    command = None
    version = None
    if ready:
        lake_path = Path(str(lake["path"]))
        args = ("env", "lean", "--version")
        launched, version = run_version(lake_path, args)
        ready = ready and launched
        command = command_text(lake_path, args)
    elif configs or mentions_mathlib or package_present:
        version = (
            f"config_mentions_mathlib={mentions_mathlib}; "
            f"local_package_present={package_present}"
        )
    return {
        "name": "Mathlib environment",
        "available": ready,
        "executable": "lake" if ready else None,
        "path": str(lake["path"]) if ready else None,
        "version": version,
        "invocation": command,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / ".tooling" / "math-tools.json",
        help="Inventory path (default: repository .tooling/math-tools.json)",
    )
    args = parser.parse_args()

    probes = [
        Probe(
            "Python 3",
            (
                str(REPO_ROOT / ".venv" / "Scripts" / "python.exe"),
                str(REPO_ROOT / ".venv" / "bin" / "python"),
                "python",
                "python3",
                "py",
            ),
            ("--version",),
            lambda s: "Python 3" in s,
        ),
        Probe("SageMath", ("sage", "sage.exe"), ("--version",)),
        Probe("Singular", ("Singular", "Singular.exe"), ("--version",), extra_paths=(
            r"%SystemDrive%\msys64\mingw64\bin\Singular.exe",
            r"%SystemDrive%\msys64\ucrt64\bin\Singular.exe",
            r"%ProgramFiles%\Singular*\bin\Singular.exe")),
        Probe("GAP", ("gap", "gap.exe", "gap.bat"), ("-q", "-c", 'Print(GAPInfo.Version,"\\n"); QUIT;')),
        Probe("PARI/GP", ("gp", "gp.exe"), ("--version",), extra_paths=(r"%ProgramFiles%\PARI\gp.exe",)),
        Probe("WolframScript", ("wolframscript", "wolframscript.exe"), ("-version",), extra_paths=(
            r"%ProgramFiles%\Wolfram Research\WolframScript\wolframscript.exe",
            r"%ProgramFiles%\Wolfram Research\Mathematica\*\wolframscript.exe")),
        Probe("Macaulay2", ("M2", "M2.exe"), ("--version",)),
        Probe("Lean 4", ("lean", "lean.exe"), ("--version",), lambda s: "Lean" in s, extra_paths=(r"%USERPROFILE%\.elan\bin\lean.exe",)),
        Probe("Lake", ("lake", "lake.exe"), ("--version",), extra_paths=(r"%USERPROFILE%\.elan\bin\lake.exe",)),
        Probe("Z3", ("z3", "z3.exe"), ("-version",), extra_paths=(r"%ProgramFiles%\Z3\bin\z3.exe",)),
        Probe("cvc5", ("cvc5", "cvc5.exe"), ("--version",)),
        Probe("MiniSat", ("minisat", "minisat.exe"), ("--help",)),
        Probe("Kissat", ("kissat", "kissat.exe"), ("--version",)),
        Probe("CaDiCaL", ("cadical", "cadical.exe"), ("--version",)),
        Probe("CryptoMiniSat", ("cryptominisat5", "cryptominisat5.exe"), ("--version",)),
        Probe("DRAT-trim", ("drat-trim", "drat-trim.exe"), ("-h",)),
        Probe("nauty geng", ("geng", "geng.exe"), ("-help",)),
        Probe("plantri", ("plantri", "plantri.exe"), ("-h",)),
        Probe("bliss", ("bliss", "bliss.exe"), ("--version",)),
        Probe("C compiler", ("cl", "clang", "gcc", "cc"), ("--version",), extra_paths=(
            r"%SystemDrive%\msys64\ucrt64\bin\gcc.exe",
            r"%SystemDrive%\msys64\mingw64\bin\gcc.exe")),
        Probe("C++ compiler", ("cl", "clang++", "g++", "c++"), ("--version",), extra_paths=(
            r"%SystemDrive%\msys64\ucrt64\bin\g++.exe",
            r"%SystemDrive%\msys64\mingw64\bin\g++.exe")),
    ]

    tools = [inventory_probe(probe) for probe in probes]
    wsl_fallbacks = {
        "SageMath": ("sage", ("--version",)),
        "Singular": ("Singular", ("--version",)),
        "GAP": ("gap", ("-q", "-c", 'Print(GAPInfo.Version,"\\n"); QUIT;')),
        "PARI/GP": ("gp", ("--version",)),
        "Z3": ("z3", ("-version",)),
        "CaDiCaL": ("cadical", ("--version",)),
    }
    for index, tool in enumerate(tools):
        fallback = wsl_fallbacks.get(str(tool["name"]))
        if tool["available"] or fallback is None:
            continue
        binary, version_args = fallback
        wsl_tool = detect_wsl_sage_tool(str(tool["name"]), binary, version_args)
        if wsl_tool is not None:
            tools[index] = wsl_tool
    wsl_user_fallbacks = {
        "cvc5": ("cvc5", ("--version",)),
        "MiniSat": ("minisat", ("--help",)),
        "Kissat": ("kissat", ("--version",)),
        "CryptoMiniSat": ("cryptominisat5", ("--version",)),
    }
    for index, tool in enumerate(tools):
        fallback = wsl_user_fallbacks.get(str(tool["name"]))
        if tool["available"] or fallback is None:
            continue
        binary, version_args = fallback
        wsl_tool = detect_wsl_user_tool(str(tool["name"]), binary, version_args)
        if wsl_tool is not None:
            tools[index] = wsl_tool
    wsl_path_fallbacks = {
        "Macaulay2": ("M2", ("--version",)),
        "nauty geng": ("geng", ("-help",)),
        "plantri": ("plantri", ("-h",)),
        "bliss": ("bliss", ("--version",)),
        "DRAT-trim": ("drat-trim", ("-h",)),
    }
    for index, tool in enumerate(tools):
        fallback = wsl_path_fallbacks.get(str(tool["name"]))
        if tool["available"] or fallback is None:
            continue
        binary, version_args = fallback
        wsl_tool = detect_wsl_path_tool(str(tool["name"]), binary, version_args)
        if wsl_tool is not None:
            tools[index] = wsl_tool
    lean = next(tool for tool in tools if tool["name"] == "Lean 4")
    lake = next(tool for tool in tools if tool["name"] == "Lake")
    python = next(tool for tool in tools if tool["name"] == "Python 3")
    tools.insert(1, detect_python_module(python, "SymPy", "sympy"))
    tools.insert(2, detect_python_module(python, "NetworkX", "networkx"))
    tools.insert(11, detect_mathlib(lake, lean))

    logical_cpus = os.cpu_count()
    try:
        affinity_cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        affinity_cpus = logical_cpus
    host_python_acceleration = (
        detect_python_acceleration(str(python["path"]))
        if python["available"] and python["path"]
        else {
            "probe_succeeded": False,
            "available": False,
            "modules": {},
            "error": "python unavailable",
        }
    )
    host_memory_total, host_memory_available = physical_memory_bytes()
    compute_resources = {
        "host": {
            "logical_cpus": logical_cpus,
            "affinity_cpus": affinity_cpus,
            "memory_total_bytes": host_memory_total,
            "memory_available_bytes": host_memory_available,
            "nvidia": detect_nvidia_gpus(),
            "python_acceleration": host_python_acceleration,
        },
        "wsl": detect_wsl_resources(),
    }

    payload = {
        "schema_version": 3,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository_root": str(REPO_ROOT),
        "platform": platform.platform(),
        "compute_resources": compute_resources,
        "tools": tools,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {output}")
    for tool in tools:
        marker = "yes" if tool["available"] else "no"
        print(f"{marker:>3}  {tool['name']}: {tool['version'] or 'not found'}")
    host = compute_resources["host"]
    memory = host["memory_total_bytes"]
    memory_gib = f"{int(memory) / 2**30:.2f} GiB" if memory else "unknown"
    print(
        f"CPU  host logical={host['logical_cpus']} affinity={host['affinity_cpus']}; "
        f"memory={memory_gib}"
    )
    nvidia = host["nvidia"]
    for device in nvidia["devices"]:
        print(
            f"GPU  {device['index']}: {device['name']}; "
            f"free={device['memory_free_mib']} MiB/{device['memory_total_mib']} MiB; "
            f"compute={device['compute_capability']}"
        )
    wsl = compute_resources["wsl"]
    if wsl["available"]:
        print(
            f"WSL  logical={wsl['logical_cpus']}; "
            f"memory={int(wsl['memory_total_bytes']) / 2**30:.2f} GiB; "
            f"gpu_bridge={wsl['gpu_bridge_present']}"
        )
    else:
        print(f"WSL  resource probe unavailable: {wsl['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
