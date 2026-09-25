"""Upgrade Tau with the package manager that owns its environment.

使用拥有当前环境的包管理器升级 Tau。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from subprocess import CompletedProcess, run
from typing import Literal

from tau_coding.update_check import PYPI_PACKAGE_NAME, fetch_latest_pypi_version

CommandRunner = Callable[..., CompletedProcess[str]]
DetachedLauncher = Callable[..., object]
ExecutableFinder = Callable[[str], str | None]
LatestVersionFetcher = Callable[[], str | None]
InstallMethod = Literal["uv-tool", "uv-pip", "pipx", "pip"]


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """Result of trying to upgrade Tau.

    尝试升级 Tau 的结果。
    """

    command: tuple[str, ...] | None
    stdout: str = ""
    stderr: str = ""
    failures: tuple[str, ...] = ()
    deferred: bool = False

    @property
    def succeeded(self) -> bool:
        """Return whether an update command completed successfully.

        返回更新命令是否成功完成。
        """
        return self.command is not None


def update_tau(
    *,
    runner: CommandRunner = run,
    python_executable: str | None = None,
    environment_prefix: Path | None = None,
    direct_url: str | None = None,
    installer: str | None = None,
    inspect_distribution: bool = True,
    latest_version_fetcher: LatestVersionFetcher = fetch_latest_pypi_version,
    platform_name: str | None = None,
    detached_launcher: DetachedLauncher = subprocess.Popen,
    executable_finder: ExecutableFinder = shutil.which,
    parent_pid: int | None = None,
    handoff_directory: Path | None = None,
) -> UpdateResult:
    """Upgrade Tau with the installer that owns the active environment.

    使用拥有当前活动环境的安装器升级 Tau。

    Python distributions record their installer in ``INSTALLER``. uv and pipx
    tool environments also leave ownership receipts. Managed, editable,
    direct-URL, and unrecognized installations stop with instructions instead
    of trying unrelated package managers.

    Python 发行版会在 ``INSTALLER`` 中记录安装器。uv 和 pipx 工具环境也会留下
    所有权凭据。受管理、可编辑、直接 URL 及无法识别的安装会停止并给出说明，
    而不会尝试无关的包管理器。
    """
    prefix = (environment_prefix or Path(sys.prefix)).resolve()
    if inspect_distribution:
        direct_url = _installed_direct_url()
        installer = _installed_installer()
    if direct_url:
        return _failure(
            "Tau was installed from a local or direct URL, so it cannot be safely "
            f"updated from PyPI. Reinstall it from its original source: {direct_url}"
        )

    method = detect_install_method(prefix, installer=installer)
    if method is None:
        if (prefix / "conda-meta").is_dir():
            return _failure(
                "Tau is installed in a Conda/Pixi-managed environment. "
                "Update it with the manager that created that environment."
            )
        installed_by = installer or "unknown"
        return _failure(
            "Could not identify a supported installer for this Tau environment "
            f"({prefix}). Package metadata reports: {installed_by}."
        )

    latest_version: str | None = None
    if method == "uv-tool":
        try:
            latest_version = latest_version_fetcher()
        except Exception as exc:  # noqa: BLE001 - report update lookup failures to the user
            return _failure(f"Could not determine the latest Tau version from PyPI: {exc}")
        if latest_version is None:
            return _failure("Could not determine the latest Tau version from PyPI.")

    executable = python_executable or sys.executable
    command = _update_command(method, executable, latest_version=latest_version)
    if (platform_name or sys.platform) == "win32" and method == "uv-tool":
        return _handoff_windows_update(
            command,
            launcher=detached_launcher,
            executable_finder=executable_finder,
            parent_pid=parent_pid or os.getpid(),
            handoff_directory=handoff_directory,
        )

    completed = _run(runner, command)
    if isinstance(completed, str):
        return _failure(f"{' '.join(command)}: {completed}")
    if completed.returncode != 0:
        return _failure(f"{' '.join(command)}: {_result_detail(completed)}")
    return UpdateResult(
        command=command,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def detect_install_method(prefix: Path, *, installer: str | None = None) -> InstallMethod | None:
    """Detect installer ownership from receipts and distribution metadata.

    从凭据和发行版元数据检测安装器所有权。
    """
    if (prefix / "uv-receipt.toml").is_file():
        return "uv-tool"
    if (prefix / "pipx_metadata.json").is_file():
        return "pipx"
    normalized_installer = installer.strip().lower() if installer else None
    if normalized_installer == "uv":
        return "uv-pip"
    if normalized_installer == "pip":
        return "pip"
    return None


def _update_command(
    method: InstallMethod,
    python_executable: str,
    *,
    latest_version: str | None = None,
) -> tuple[str, ...]:
    """Build the installer-specific command used to upgrade Tau.

    构建用于升级 Tau 的安装器专用命令。
    """
    if method == "uv-tool":
        if latest_version is None:
            raise ValueError("latest_version is required for uv tool updates")
        return ("uv", "tool", "install", f"{PYPI_PACKAGE_NAME}@{latest_version}")
    if method == "uv-pip":
        return (
            "uv",
            "pip",
            "install",
            "--python",
            python_executable,
            "--upgrade",
            PYPI_PACKAGE_NAME,
        )
    if method == "pipx":
        return ("pipx", "upgrade", PYPI_PACKAGE_NAME)
    return (python_executable, "-m", "pip", "install", "--upgrade", PYPI_PACKAGE_NAME)


_WINDOWS_UPDATE_SCRIPT = """param(
    [Parameter(Mandatory=$true)][int]$ParentProcessId,
    [Parameter(Mandatory=$true)][string]$LogPath,
    [Parameter(Mandatory=$true)][string]$UpdatePayloadBase64
)

$ScriptPath = $MyInvocation.MyCommand.Path
$UpdateExitCode = 1
function Write-Log([string]$Message) {
    $Message | Add-Content -LiteralPath $LogPath -Encoding UTF8
}
try {
    $ErrorActionPreference = "Stop"
    $WaitMessage = "Waiting for Tau process $ParentProcessId to exit..."
    $WaitMessage | Set-Content -LiteralPath $LogPath -Encoding UTF8
    $ParentProcess = $null
    try {
        $ParentProcess = Get-Process -Id $ParentProcessId -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -like "NoProcessFoundForGivenId*") {
            Write-Log "Tau process $ParentProcessId has already exited."
        } else {
            $Detail = $_.Exception.Message
            throw "Could not inspect Tau process $ParentProcessId. Update aborted: $Detail"
        }
    }
    if ($null -ne $ParentProcess) {
        try {
            Wait-Process -InputObject $ParentProcess -ErrorAction Stop
        } catch {
            $Detail = $_.Exception.Message
            throw "Could not wait for Tau process $ParentProcessId to exit. Update aborted: $Detail"
        }
    }
    $PayloadJson = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($UpdatePayloadBase64)
    )
    $UpdatePayload = ConvertFrom-Json -InputObject $PayloadJson
    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = [string]$UpdatePayload.executable
    $StartInfo.Arguments = [string]$UpdatePayload.arguments
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $UpdateProcess = New-Object System.Diagnostics.Process
    $UpdateProcess.StartInfo = $StartInfo
    if (-not $UpdateProcess.Start()) {
        throw "Could not start the update command. Update aborted."
    }
    $StandardOutputTask = $UpdateProcess.StandardOutput.ReadToEndAsync()
    $StandardErrorTask = $UpdateProcess.StandardError.ReadToEndAsync()
    $UpdateProcess.WaitForExit()
    $StandardOutput = $StandardOutputTask.GetAwaiter().GetResult()
    $StandardError = $StandardErrorTask.GetAwaiter().GetResult()
    if ($StandardOutput) {
        Write-Log $StandardOutput.TrimEnd("`r", "`n")
    }
    if ($StandardError) {
        Write-Log $StandardError.TrimEnd("`r", "`n")
    }
    $UpdateExitCode = $UpdateProcess.ExitCode
    $UpdateProcess.Dispose()
    Write-Log "Update command exited with code $UpdateExitCode."
} catch {
    Write-Log ($_ | Out-String)
} finally {
    Remove-Item -LiteralPath $ScriptPath -Force -ErrorAction SilentlyContinue
}
exit $UpdateExitCode
"""


def _handoff_windows_update(
    command: tuple[str, ...],
    *,
    launcher: DetachedLauncher,
    executable_finder: ExecutableFinder,
    parent_pid: int,
    handoff_directory: Path | None,
) -> UpdateResult:
    """Stage a detached PowerShell process that updates Tau after exit.

    暂存一个分离的 PowerShell 进程，在 Tau 退出后执行更新。
    """
    powershell = executable_finder("powershell.exe") or executable_finder("pwsh.exe")
    if powershell is None:
        return _failure("Could not find PowerShell to safely finish the Windows update.")

    owns_update_dir = handoff_directory is None
    update_dir: Path | None = None
    script_path: Path | None = None
    try:
        if owns_update_dir:
            update_dir = Path(tempfile.mkdtemp(prefix="tau-update-"))
        else:
            update_dir = handoff_directory
            assert update_dir is not None
            update_dir.mkdir(parents=True, exist_ok=True)
        script_path = update_dir / "update.ps1"
        log_path = update_dir / "update.log"
        script_path.write_text(_WINDOWS_UPDATE_SCRIPT, encoding="utf-8")
    except OSError as exc:
        _cleanup_windows_handoff(update_dir, script_path, remove_directory=owns_update_dir)
        return _failure(f"Could not stage the detached Windows updater: {exc}")

    update_payload = {
        "executable": command[0],
        "arguments": _windows_command_line(command[1:]),
    }
    encoded_payload = base64.b64encode(
        json.dumps(update_payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    powershell_command = (
        powershell,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-ParentProcessId",
        str(parent_pid),
        "-LogPath",
        str(log_path),
        "-UpdatePayloadBase64",
        encoded_payload,
    )
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    try:
        launcher(
            powershell_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        _cleanup_windows_handoff(update_dir, script_path, remove_directory=owns_update_dir)
        return _failure(f"Could not start the detached Windows updater: {exc}")
    return UpdateResult(
        command=command,
        stdout=(
            "Tau update is scheduled and will start after this process exits. "
            f"Progress will be written to {log_path}."
        ),
        deferred=True,
    )


def _quote_windows_argument(argument: str) -> str:
    """Quote one argv element using the documented Microsoft C runtime rules.

    使用有文档说明的 Microsoft C 运行时规则引用一个 argv 元素。
    """
    quoted = ['"']
    backslashes = 0
    for character in argument:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"':
            quoted.append("\\" * (backslashes * 2 + 1))
            quoted.append('"')
        else:
            quoted.append("\\" * backslashes)
            quoted.append(character)
        backslashes = 0
    quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(quoted)


def _windows_command_line(arguments: tuple[str, ...]) -> str:
    """Build the argv tail passed directly to CreateProcess by ProcessStartInfo.

    构建由 ProcessStartInfo 直接传给 CreateProcess 的 argv 尾部。
    """
    return " ".join(_quote_windows_argument(argument) for argument in arguments)


def _cleanup_windows_handoff(
    update_dir: Path | None,
    script_path: Path | None,
    *,
    remove_directory: bool,
) -> None:
    """Remove staged Windows update artifacts after a failed handoff.

    在交接失败后移除暂存的 Windows 更新产物。
    """
    if script_path is not None:
        with suppress(OSError):
            script_path.unlink(missing_ok=True)
    if remove_directory and update_dir is not None:
        with suppress(OSError):
            update_dir.rmdir()


def _installed_direct_url() -> str | None:
    """Return the direct installation source recorded by package metadata.

    返回包元数据记录的直接安装源。
    """
    raw = _distribution_file("direct_url.json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "an unrecognized direct source"
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) and url else "an unrecognized direct source"


def _installed_installer() -> str | None:
    """Return the installer name recorded by package metadata.

    返回包元数据记录的安装器名称。
    """
    raw = _distribution_file("INSTALLER")
    if not raw:
        return None
    return raw.strip() or None


def _distribution_file(filename: str) -> str | None:
    """Read one metadata file from the installed Tau distribution.

    从已安装的 Tau 发行版读取一个元数据文件。
    """
    try:
        return distribution(PYPI_PACKAGE_NAME).read_text(filename)
    except PackageNotFoundError:
        return None


def _run(runner: CommandRunner, command: tuple[str, ...]) -> CompletedProcess[str] | str:
    """Run an update command and convert launch errors to safe text.

    运行更新命令，并将启动错误转换为安全文本。
    """
    try:
        return runner(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return str(exc)


def _result_detail(result: CompletedProcess[str]) -> str:
    """Extract concise diagnostic detail from a failed command result.

    从失败的命令结果中提取简洁诊断详情。
    """
    return result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"


def _failure(message: str) -> UpdateResult:
    """Create a failed update result with one user-facing message.

    使用一条面向用户的消息创建失败更新结果。
    """
    return UpdateResult(command=None, failures=(message,))
