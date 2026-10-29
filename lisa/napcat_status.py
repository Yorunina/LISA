"""Read-only local NapCat checks; never authenticate or change the QQ session."""

import json
import os
import subprocess
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from lisa.settings import ROOT


def _usable_token(value):
    if (
        isinstance(value, str) and value.strip()
        and value not in {"napcat", "generated-by-init"} and len(value) <= 4096
    ):
        return value
    return None


def management_url(settings):
    """Read the saved token only for an authenticated maintainer's explicit navigation."""
    host = urlsplit(settings.napcat_webui_probe_url).hostname or ""
    try:
        local = host == "localhost" or ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    token = None
    if local:
        try:
            path = ROOT / "runtime/napcat/config/webui.json"
            with path.open(encoding="utf-8-sig") as handle:
                contents = handle.read(65537)
            if len(contents) <= 65536:
                payload = json.loads(contents)
                if isinstance(payload, dict):
                    token = _usable_token(payload.get("token"))
        except (OSError, ValueError):
            pass
    token = token or _usable_token(settings.napcat_webui_token.get_secret_value())
    # Missing/invalid configuration keeps the native manual-login flow available.
    return settings.napcat_webui_url + ("?" + urlencode({"token": token}) if token else "")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def process_running():
    """Recognize this installation's loader, including a changed/stale recorded PID."""
    if os.name != "nt":
        return None
    node = ROOT / "runtime/tools/napcat/node.exe"
    if not node.is_file():
        return False
    command = (
        "$ErrorActionPreference='Stop'; "
        "$items=@(Get-Process -Name node -ErrorAction SilentlyContinue); "
        "$found=@($items | Where-Object { "
        "[string]::Equals($_.Path,$env:LISA_NAPCAT_PROBE_NODE,"
        "[StringComparison]::OrdinalIgnoreCase) }); "
        "$uncertain=@($items | Where-Object { !$_.Path }); "
        "@{running=if($found.Count -gt 0){$true}"
        "elseif($uncertain.Count -gt 0){$null}else{$false}} | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            env={**os.environ, "LISA_NAPCAT_PROBE_NODE": str(node)},
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=4, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(result.stdout)
        value = payload.get("running") if isinstance(payload, dict) else None
        return value if isinstance(value, bool) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def webui_reachable(url):
    """Check the configured page without a token, redirects, proxies, or response logging."""
    try:
        opener = build_opener(ProxyHandler({}), NoRedirects())
        with opener.open(Request(url, headers={"User-Agent": "LISA-status"}), timeout=2) as reply:
            # Read only the start of the static HTML, not an authenticated API response.
            content = reply.read(8192).decode("utf-8", errors="replace")
            if reply.status == 200 and "<title>NapCat WebUI</title>" in content:
                return True
            return None  # Another page/proxy login does not prove NapCat is running.
    except HTTPError:
        return None  # Authentication/redirect/proxy errors do not establish process failure.
    except (URLError, OSError, ValueError):
        return False


def snapshot(settings):
    running = process_running()
    reachable = webui_reachable(settings.napcat_webui_probe_url)
    # A genuine NapCat page also works when the loader is on a different host.
    status = "ok" if running is True or reachable is True else (
        "offline" if running is False and reachable is False else "unknown"
    )
    return {
        "component": "napcat", "status": status, "observed_at": datetime.now(UTC),
        "details": {"process_running": running, "webui_reachable": reachable},
    }
