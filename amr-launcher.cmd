@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Autonomous Math AI

rem Generic bootstrap only. Project paths and research configuration never live here.
if not defined AMR_HARNESS_ROOT set "AMR_HARNESS_ROOT=%~dp0."
if not defined AMR_VENV_ROOT set "AMR_VENV_ROOT=%LOCALAPPDATA%\autonomous-math-ai-venv"
rem 0 reuses the local harness installation; set 1 only while the shared venv is idle.
if not defined AMR_REFRESH_HARNESS set "AMR_REFRESH_HARNESS=0"
set "AMR_LAUNCHER_INTERACTIVE=0"
if "%~1"=="" set "AMR_LAUNCHER_INTERACTIVE=1"

if not exist "%AMR_HARNESS_ROOT%\pyproject.toml" goto harness_error
if not exist "%AMR_VENV_ROOT%\Scripts\python.exe" call :create_venv
if errorlevel 1 goto setup_failed

set "AMR_EXE=%AMR_VENV_ROOT%\Scripts\amr.exe"
if "%AMR_REFRESH_HARNESS%"=="1" call :install_harness
if errorlevel 1 goto setup_failed
if not exist "%AMR_EXE%" call :install_harness
if errorlevel 1 goto setup_failed

"%AMR_EXE%" launcher %*
set "AMR_EXIT=%ERRORLEVEL%"
echo.
if "%AMR_EXIT%"=="0" (
  echo Launcher closed normally.
) else (
  echo Launcher exited with code %AMR_EXIT%.
)
if "%AMR_LAUNCHER_INTERACTIVE%"=="1" pause
exit /b %AMR_EXIT%

:create_venv
echo Creating virtual environment: %AMR_VENV_ROOT%
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -m venv "%AMR_VENV_ROOT%"
) else (
  python -m venv "%AMR_VENV_ROOT%"
)
exit /b %ERRORLEVEL%

:install_harness
call :ensure_venv_idle
if errorlevel 1 exit /b %ERRORLEVEL%
echo Installing local harness: %AMR_HARNESS_ROOT%
"%AMR_VENV_ROOT%\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade "%AMR_HARNESS_ROOT%"
exit /b %ERRORLEVEL%

:ensure_venv_idle
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$root=[IO.Path]::GetFullPath($env:AMR_VENV_ROOT).TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar; $busy=@(Get-Process -ErrorAction SilentlyContinue | Where-Object { try { $processPath=$_.Path } catch { $processPath=$null }; $processPath -and [IO.Path]::GetFullPath($processPath).StartsWith($root,[StringComparison]::OrdinalIgnoreCase) }); if ($busy) { $busy | Select-Object Id,ProcessName,Path | Format-Table -AutoSize; exit 32 }"
if errorlevel 1 (
  echo Refusing to install or upgrade while the shared virtual environment is in use.
  exit /b 32
)
exit /b 0

:harness_error
echo Harness checkout not found: %AMR_HARNESS_ROOT%\pyproject.toml
if "%AMR_LAUNCHER_INTERACTIVE%"=="1" pause
exit /b 2

:setup_failed
echo Failed to prepare the virtual environment or install the harness.
if "%AMR_LAUNCHER_INTERACTIVE%"=="1" pause
exit /b 1
