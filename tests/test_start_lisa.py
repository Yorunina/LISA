"""Launcher checks use unittest directly, without a database or real service startup."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "start_lisa", Path(__file__).resolve().parents[1] / "scripts/start_lisa.py"
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_only_this_checkouts_services_are_reused(self):
        python = str(launcher.PYTHON)
        processes = [
            {"ProcessId": 1, "ExecutablePath": python,
             "CommandLine": f'"{python}" -m lisa.worker'},
            {"ProcessId": 2, "ParentProcessId": 1, "ExecutablePath": "C:/Python/python.exe",
             "CommandLine": "python.exe -m lisa.worker"},
            {"ProcessId": 3, "ExecutablePath": "C:/Other/python.exe",
             "CommandLine": "python.exe -m lisa.gateway"},
            {"ProcessId": 4, "ExecutablePath": python,
             "CommandLine": f'"{python}" -m uvicorn lisa.app:create_app --factory'},
        ]
        self.assertEqual(launcher.service_roles(processes), {"worker", "api"})

    def test_matching_module_prefix_is_not_a_service(self):
        self.assertEqual(launcher.service_roles([
            {"ProcessId": 1, "ExecutablePath": str(launcher.PYTHON),
             "CommandLine": "python -m lisa.worker_test"},
        ]), set())

    def test_build_detects_changed_source_and_missing_output(self):
        with tempfile.TemporaryDirectory(dir=launcher.ROOT / "runtime") as folder:
            root = Path(folder)
            with patch.object(launcher, "ROOT", root):
                self.assertTrue(launcher.build_needed())
                (root / "web").mkdir()
                for name in ("index.html", "package.json", "package-lock.json",
                             "vite.config.ts", "tsconfig.json", "web/main.tsx"):
                    (root / name).write_text("source", encoding="utf-8")
                (root / "runtime/dist").mkdir(parents=True)
                index = root / "runtime/dist/index.html"
                index.write_text("built", encoding="utf-8")
                self.assertFalse(launcher.build_needed())
                os.utime(root / "web/main.tsx", (index.stat().st_mtime + 5,) * 2)
                self.assertTrue(launcher.build_needed())

    def test_simultaneous_launches_are_locked(self):
        with tempfile.TemporaryDirectory(dir=launcher.ROOT / "runtime") as folder:
            with patch.object(launcher, "ROOT", Path(folder)):
                with launcher.startup_lock():
                    with self.assertRaisesRegex(RuntimeError, "正在运行"):
                        with launcher.startup_lock():
                            self.fail("Second launcher obtained the lock")
                with launcher.startup_lock():
                    pass  # The OS lock is released even though the file remains.

    def mock_launch(self, roles=()):
        stack = ExitStack()
        self.addCleanup(stack.close)
        mocks = {}
        for name in ("startup_lock", "preflight", "dependency", "check_schema", "run",
                     "build_needed", "start_service", "wait_for_web", "running_services"):
            mocks[name] = stack.enter_context(patch.object(launcher, name))
        stack.enter_context(patch.object(launcher.time, "sleep"))
        stack.enter_context(patch.object(launcher.webbrowser, "open"))
        stack.enter_context(patch("builtins.print"))
        mocks["preflight"].return_value = set(roles)
        mocks["running_services"].return_value = set(launcher.COMMANDS)
        mocks["build_needed"].return_value = False
        mocks["start_service"].return_value.poll.return_value = None
        return mocks

    def test_check_only_does_not_start_build_migrate_or_open_browser(self):
        mocks = self.mock_launch()
        launcher.launch(check_only=True)
        for name in ("dependency", "run", "check_schema", "start_service"):
            mocks[name].assert_not_called()
        launcher.webbrowser.open.assert_not_called()

    def test_cold_start_orders_migration_before_background_services(self):
        mocks = self.mock_launch()
        events = []
        mocks["run"].side_effect = lambda *args: events.append("migrate")
        mocks["start_service"].side_effect = lambda role, stamp: (
            events.append(role) or Mock(poll=Mock(return_value=None))
        )
        launcher.launch(no_browser=True)
        self.assertEqual(events, ["migrate", "api", "worker", "gateway"])
        mocks["dependency"].assert_any_call("StartQQ")
        launcher.webbrowser.open.assert_not_called()

    def test_repeat_start_reuses_all_processes_and_checks_schema(self):
        mocks = self.mock_launch(launcher.COMMANDS)
        launcher.launch()
        mocks["start_service"].assert_not_called()
        mocks["run"].assert_not_called()
        mocks["check_schema"].assert_called_once()
        launcher.webbrowser.open.assert_called_once_with(launcher.URL)

    def test_partial_start_only_fills_missing_services(self):
        mocks = self.mock_launch({"worker"})
        launcher.launch(no_browser=True)
        self.assertEqual([call.args[0] for call in mocks["start_service"].call_args_list],
                         ["api", "gateway"])
        mocks["run"].assert_not_called()

    def test_old_schema_does_not_start_services_or_migrate_running_application(self):
        mocks = self.mock_launch({"worker"})
        mocks["check_schema"].side_effect = subprocess.CalledProcessError(1, "schema-check")
        with self.assertRaises(subprocess.CalledProcessError):
            launcher.launch()
        mocks["start_service"].assert_not_called()
        mocks["run"].assert_not_called()
        launcher.webbrowser.open.assert_not_called()

    def test_failed_service_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "worker"):
            launcher.check_children({"worker": Mock(poll=Mock(return_value=1))})

    def test_background_process_writes_logs_and_survives_launcher_handle_close(self):
        with tempfile.TemporaryDirectory(dir=launcher.ROOT / "runtime") as folder:
            with patch.object(launcher, "ROOT", Path(folder)):
                with patch.dict(launcher.COMMANDS, {
                    "probe": ["-c", "import time; print('ready', flush=True); time.sleep(1)"],
                }):
                    process = launcher.start_service("probe", "isolated")
                    try:
                        self.assertIsNone(process.poll())
                        self.assertEqual(process.wait(timeout=10), 0)
                        self.assertEqual(
                            (Path(folder) / "runtime/logs/probe-isolated.out.log")
                            .read_text(encoding="utf-8").strip(), "ready",
                        )
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=5)

    def test_foreign_port_stops_startup(self):
        with patch.object(Path, "is_file", return_value=True):
            with patch.object(launcher, "running_services", return_value=set()):
                with patch.object(launcher, "port_busy", return_value=True):
                    with self.assertRaisesRegex(RuntimeError, "8000"):
                        launcher.preflight()


if __name__ == "__main__":
    unittest.main()
