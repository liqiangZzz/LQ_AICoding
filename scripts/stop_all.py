from __future__ import annotations

import subprocess
import sys
import re
from collections.abc import Iterable


DEFAULT_PORTS = (2024, 3000)

IS_WINDOWS = sys.platform.startswith("win")


def _run_powershell(command: str) -> subprocess.CompletedProcess[str]:
    """执行一段只用于进程查询/停止的 PowerShell 命令。

    这里不用 shell=True，避免命令被额外解释；所有动态值只来自固定端口数字。
    """

    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )


def _find_pids_windows(ports: Iterable[int]) -> list[int]:
    """Windows：优先用 Get-NetTCPConnection，再兜底 netstat。"""

    port_list = ",".join(str(port) for port in ports)
    command = (
        f"Get-NetTCPConnection -LocalPort {port_list} -ErrorAction SilentlyContinue "
        "| Where-Object { $_.State -eq 'Listen' } "
        "| Select-Object -ExpandProperty OwningProcess -Unique"
    )
    result = _run_powershell(command)

    pids: list[int] = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pids.append(int(line))
            except ValueError:
                continue
    if pids:
        return sorted(set(pids))

    # 某些 Windows/PyCharm 组合下 Get-NetTCPConnection 可能查不到，
    # 但 netstat 仍然能看到 LISTENING 记录，所以这里做一次兜底解析。
    netstat = subprocess.run(
        ["netstat", "-ano"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if netstat.returncode != 0:
        return []
    port_set = {int(port) for port in ports}
    for line in netstat.stdout.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local_address = parts[1]
        pid_text = parts[-1]
        match = re.search(r":(\d+)$", local_address)
        if not match:
            continue
        if int(match.group(1)) not in port_set:
            continue
        try:
            pids.append(int(pid_text))
        except ValueError:
            continue
    return sorted(set(pids))


def _find_pids_posix(ports: Iterable[int]) -> list[int]:
    """macOS/Linux：用 lsof 查找监听端口的 PID。"""

    pids: list[int] = []
    for port in ports:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pids.append(int(line))
            except ValueError:
                continue
    return sorted(set(pids))


def find_pids_by_ports(ports: Iterable[int] = DEFAULT_PORTS) -> list[int]:
    """根据端口查找监听进程 PID。

    start_all.py 默认占用：
    - 2024：FastAPI/Uvicorn 后端
    - 3000：Vite 前端
    """

    if IS_WINDOWS:
        return _find_pids_windows(ports)
    return _find_pids_posix(ports)


def _stop_pids_windows(pids: Iterable[int]) -> list[int]:
    stopped: list[int] = []
    for pid in sorted(set(pids)):
        if pid <= 0:
            continue
        command = f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"
        _run_powershell(command)
        stopped.append(pid)
    return stopped


def _stop_pids_posix(pids: Iterable[int]) -> list[int]:
    stopped: list[int] = []
    for pid in sorted(set(pids)):
        if pid <= 0:
            continue
        subprocess.run(["kill", "-9", str(pid)], stderr=subprocess.PIPE)
        stopped.append(pid)
    return stopped


def stop_pids(pids: Iterable[int]) -> list[int]:
    """停止指定 PID，并返回实际发出停止命令的 PID 列表。"""

    if IS_WINDOWS:
        return _stop_pids_windows(pids)
    return _stop_pids_posix(pids)


def stop_ports(ports: Iterable[int] = DEFAULT_PORTS) -> list[int]:
    """停止指定端口上的监听进程。"""

    pids = find_pids_by_ports(ports)
    return stop_pids(pids)


def main() -> None:
    """清理课程项目常用的后端和前端服务端口。"""

    ports = DEFAULT_PORTS
    stopped = stop_ports(ports)
    if stopped:
        print(f"已停止端口 {ports} 对应进程：{stopped}")
    else:
        print(f"端口 {ports} 当前没有监听进程。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)