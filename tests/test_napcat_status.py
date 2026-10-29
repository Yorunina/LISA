import asyncio
import json
import subprocess
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr, ValidationError

from lisa import napcat_status, runtime_status


def test_saved_token_is_read_on_click_and_rotation_does_not_need_another_copy(
    settings, tmp_path, monkeypatch
):
    monkeypatch.setattr(napcat_status, "ROOT", tmp_path)
    config = tmp_path / "runtime/napcat/config/webui.json"
    config.parent.mkdir(parents=True)
    configured = settings.model_copy(update={
        "napcat_webui_url": "https://console.example.test/webui",
        "napcat_webui_probe_url": "http://127.0.0.1:6099/webui",
        "napcat_webui_token": SecretStr("synthetic-fallback"),
    })
    for token in ("synthetic +/?&= token", "synthetic-rotated-token"):
        config.write_text(json.dumps({"token": token}), encoding="utf-8")
        target = urlsplit(napcat_status.management_url(configured))
        assert target.netloc == "console.example.test"
        assert parse_qs(target.query) == {"token": [token]}


def test_remote_napcat_does_not_receive_an_unrelated_local_token(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(napcat_status, "ROOT", tmp_path)
    config = tmp_path / "runtime/napcat/config/webui.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"token": "unrelated-local-token"}), encoding="utf-8")
    configured = settings.model_copy(update={
        "napcat_webui_probe_url": "https://remote.example.test/webui",
        "napcat_webui_url": "https://remote.example.test/webui",
        "napcat_webui_token": SecretStr("synthetic-remote-token"),
    })
    assert parse_qs(urlsplit(napcat_status.management_url(configured)).query) == {
        "token": ["synthetic-remote-token"],
    }
    configured.napcat_webui_token = SecretStr("")
    assert napcat_status.management_url(configured) == configured.napcat_webui_url


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "http://user:password@localhost:6099/webui",
    "http://localhost:6099/webui?token=private", "http://localhost/webui#private",
    "http://localhost:70000/webui", "http://localhost/\nwebui",
])
def test_management_urls_cannot_transport_credentials(settings, value):
    for field in ("napcat_webui_url", "napcat_webui_probe_url"):
        with pytest.raises(ValidationError):
            type(settings)(**{**settings.model_dump(), field: value})


def test_process_probe_is_read_only_and_handles_unreadable_processes(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps({"running": None}))

    monkeypatch.setattr(napcat_status.subprocess, "run", run)
    assert napcat_status.process_running() is None
    assert len(commands) == 1
    command, options = commands[0]
    assert "Get-Process -Name node" in command[-1]
    assert "Stop-Process" not in command[-1] and "Start-Process" not in command[-1]
    assert options["env"]["LISA_NAPCAT_PROBE_NODE"].endswith("napcat\\node.exe")
    assert options["timeout"] == 4

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic", 4)

    monkeypatch.setattr(napcat_status.subprocess, "run", timeout)
    assert napcat_status.process_running() is None


def test_webui_probe_checks_identity_without_authenticating_or_following_redirects(monkeypatch):
    class Reply(BytesIO):
        status = 200

    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Reply(b"<html><title>NapCat WebUI</title></html>")

    monkeypatch.setattr(napcat_status, "build_opener", lambda *args: Opener())
    assert napcat_status.webui_reachable("http://127.0.0.1:6099/webui") is True
    assert requests[0].get_header("Authorization") is None
    assert requests[0].get_header("Cookie") is None
    assert napcat_status.NoRedirects().redirect_request(None, None, 302, "", {}, "") is None

    monkeypatch.setattr(Reply, "status", 200)
    monkeypatch.setattr(Opener, "open", lambda *args, **kwargs: Reply(b"another app"))
    assert napcat_status.webui_reachable("http://127.0.0.1:6099/webui") is None


@pytest.mark.asyncio
async def test_failed_napcat_probe_does_not_stop_api_heartbeats(settings, monkeypatch):
    calls = []
    resumed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def heartbeat(engine, component, status, details):
        calls.append((component, status))
        if sum(item == ("api", "ok") for item in calls) >= 2:
            loop.call_soon_threadsafe(resumed.set)
        return True

    def broken(settings):
        raise RuntimeError("private synthetic exception must not reach caller")

    monkeypatch.setattr(runtime_status, "heartbeat", heartbeat)
    monkeypatch.setattr(napcat_status, "snapshot", broken)
    configured = settings.model_copy(update={
        "operations_maintainers": "operator", "operations_heartbeat_seconds": .01,
    })
    async with runtime_status.process_pulse(None, configured, ["api"]):
        await asyncio.wait_for(resumed.wait(), timeout=2)
    assert ("api", "offline") in calls
