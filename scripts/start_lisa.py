"""One-click Windows startup; reuse this checkout's services without restarting them."""

import argparse
import json
import msvcrt
import os
import re
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "runtime/venv/Scripts/python.exe"
URL = "http://127.0.0.1:8000"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
COMMANDS = {
    "api": [
        "-m", "uvicorn", "lisa.app:create_app", "--factory", "--host", "127.0.0.1",
        "--port", "8000", "--no-access-log",
    ],
    "worker": ["-m", "lisa.worker"],
    "gateway": ["-m", "lisa.gateway"],
}


def service_roles(processes: list[dict]) -> set[str]:
    """Recognize venv launchers and their children, including manually started services."""
    python = os.path.normcase(str(PYTHON))
    own_ids = {
        item["ProcessId"] for item in processes
        if python == os.path.normcase(item.get("ExecutablePath") or "")
        or python in os.path.normcase(item.get("CommandLine") or "")
    }
    roles = set()
    for item in processes:
        if item["ProcessId"] not in own_ids and item.get("ParentProcessId") not in own_ids:
            continue
        command = item.get("CommandLine") or ""
        for role, pattern in (
            ("api", r"\buvicorn\s+lisa\.app:create_app(?:\s|$)"),
            ("worker", r"-m\s+lisa\.worker(?:\s|$)"),
            ("gateway", r"-m\s+lisa\.gateway(?:\s|$)"),
        ):
            if re.search(pattern, command):
                roles.add(role)
    return roles


def running_services() -> set[str]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | "
         "Where-Object { $_.Name -match '^python(w)?\\.exe$' } | "
         "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) | "
         "ConvertTo-Json -Compress"],
        capture_output=True, text=True, cwd=ROOT, creationflags=NO_WINDOW, check=True,
    )
    data = json.loads(result.stdout or "[]")
    return service_roles(data if isinstance(data, list) else [data])


def port_busy() -> bool:
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex(("127.0.0.1", 8000)) == 0


def run(*args: str) -> None:
    subprocess.run(list(args), cwd=ROOT, check=True)


def dependency(action: str) -> None:
    run("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(ROOT / "scripts/dev.ps1"), action)


def build_needed() -> bool:
    index = ROOT / "runtime/dist/index.html"
    if not index.is_file():
        return True
    inputs = [ROOT / name for name in (
        "index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json",
    )]
    inputs.extend(path for path in (ROOT / "web").rglob("*") if path.is_file())
    return any(path.stat().st_mtime > index.stat().st_mtime for path in inputs)


def check_schema() -> None:
    # Never migrate beneath an existing worker/API; compare revisions read-only instead.
    run(str(PYTHON), "-c",
        "from alembic.config import Config; "
        "from alembic.script import ScriptDirectory; "
        "from alembic.runtime.migration import MigrationContext; "
        "from lisa.db import make_engine; from lisa.settings import Settings; "
        "engine=make_engine(Settings()); "
        "connection=engine.connect(); "
        "current=set(MigrationContext.configure(connection).get_current_heads()); "
        "expected=set(ScriptDirectory.from_config(Config('alembic.ini')).get_heads()); "
        "connection.close(); engine.dispose(); "
        "assert current == expected, "
        "'Schema update required. Run launcher/Stop-LISA.cmd before launcher/Start-LISA.cmd.'")


def start_service(role: str, stamp: str) -> subprocess.Popen:
    logs = ROOT / "runtime/logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f"{role}-{stamp}.out.log").open("ab") as out:
        with (logs / f"{role}-{stamp}.err.log").open("ab") as err:
            return subprocess.Popen(
                [str(PYTHON), *COMMANDS[role]], cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, creationflags=NO_WINDOW,
            )


def check_children(children: dict[str, subprocess.Popen]) -> None:
    for role, process in children.items():
        if process.poll() is not None:
            raise RuntimeError(f"{role} 启动后退出，请查看 runtime/logs/{role}-*.err.log。")


def wait_for_web(children: dict[str, subprocess.Popen], *, timeout: float = 40) -> None:
    opener = build_opener(ProxyHandler({}))  # Local health checks never use the model proxy.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_children(children)
        try:
            with opener.open(URL + "/api/health", timeout=2) as response:
                healthy = json.load(response).get("status") == "ok"
            with opener.open(URL + "/", timeout=2) as response:
                webpage = response.status == 200
            if healthy and webpage:
                return
        except (URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("网页或数据库未就绪，请查看 runtime/logs/api-*.err.log。")


@contextmanager
def startup_lock():
    path = ROOT / "runtime/startup.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("另一个 LISA 启动入口正在运行，请稍后再试。") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def preflight() -> set[str]:
    for path in (PYTHON, ROOT / "runtime/.env",
                 ROOT / "runtime/tools/postgresql/bin/pg_ctl.exe",
                 ROOT / "runtime/tools/napcat/node.exe"):
        if not path.is_file():
            raise RuntimeError(f"缺少 {path.relative_to(ROOT)}，请先运行 scripts/dev.ps1 Setup。")
    roles = running_services()
    if "api" not in roles and port_busy():
        raise RuntimeError("8000 端口已被其他进程占用，请先检查；未启动第二个 API。")
    if build_needed() and not (ROOT / "node_modules/.bin/vite.cmd").is_file():
        raise RuntimeError("缺少网页构建依赖，请先运行 scripts/dev.ps1 Setup。")
    return roles


def launch(*, no_browser: bool = False, check_only: bool = False) -> None:
    with startup_lock():
        roles = preflight()
        print("已运行：" + ("、".join(sorted(roles)) or "无应用服务"), flush=True)
        if check_only:
            print("启动条件检查通过；未启动服务、迁移数据库或调用模型。")
            return
        print("[1/4] 检查数据库与应用版本…", flush=True)
        dependency("Start")
        if roles:
            check_schema()
        else:
            run(str(PYTHON), "-m", "lisa", "migrate")
        print("[2/4] 准备网页…", flush=True)
        if build_needed():
            run("cmd.exe", "/d", "/c", "npm.cmd run build")
        print("[3/4] 启动 NapCat 与应用服务…", flush=True)
        dependency("StartQQ")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        children = {}
        for role in COMMANDS:
            if role not in roles:
                children[role] = start_service(role, stamp)
        print("[4/4] 等待网页与进程就绪…", flush=True)
        wait_for_web(children)
        time.sleep(3)
        check_children(children)
        if running_services() != set(COMMANDS):
            raise RuntimeError("应用服务不完整，请查看 runtime/logs 后重新运行入口。")
        print(f"LISA 已启动：{URL}\n后台日志：runtime/logs/", flush=True)
        print("QQ 会话失效时，请到 http://127.0.0.1:6099/webui 重新扫码。")
        if not no_browser:
            webbrowser.open(URL)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="LISA 一键启动")
    parser.add_argument("--no-browser", action="store_true", help="启动后不打开浏览器")
    parser.add_argument("--check-only", action="store_true", help="只检查启动条件，不启动服务")
    args = parser.parse_args()
    try:
        launch(no_browser=args.no_browser, check_only=args.check_only)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"启动未完成：{exc}\n已有服务保持运行，可修正问题后重试。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
