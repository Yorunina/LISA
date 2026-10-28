"""Resume native transfer from saved sources without replaying completed calls."""

import copy
import importlib.util
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MethodType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load_frozen_modules(folder):
    inputs = folder / "inputs"
    names = [
        "lisa.dialogue_skill_review",
        "scripts.benchmark_chat_distillation",
        "scripts.benchmark_chat_prompt",
        "scripts.benchmark_real_chat_prompt",
        "scripts.benchmark_dialogue_skill",
        "scripts.benchmark_dialogue_skill_native",
    ]
    for name in names:
        path = inputs / (name.replace(".", "/") + ".py")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        for key, value in list(vars(module).items()):
            if isinstance(value, Path) and value.is_relative_to(inputs):
                setattr(module, key, ROOT / value.relative_to(inputs))


async def resume(folder):
    from lisa.db import make_engine
    from lisa.dialogue_skill import compile_dialogue_skill
    from lisa.settings import Settings
    from scripts.benchmark_dialogue_skill_native import NativeSkillRun

    original = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    assert original["experiment"] == "natural_dialogue_native_transfer_v1"
    assert original["status"] == "partial" and original.get("source_drift")
    assert not original["judgments"] and original["calibration"]["passed"]
    assert all(c["state"] == "succeeded" for c in original["calls"])
    cfg = Settings()
    assert (
        cfg.model_name,
        cfg.model_base_url,
        cfg.model_reasoning_effort,
        cfg.model_api_protocol,
    ) == (original["model"], original["base_url"], original["reasoning"], original["protocol"])
    prompt = "你是LISA，正在和熟人用中文交流。\n" + compile_dialogue_skill("review")
    assert sha256(prompt.encode()).hexdigest() == original["prompt_hashes"]["review"]
    run = object.__new__(NativeSkillRun)
    run.args = SimpleNamespace(
        stage="confirm", modes=["review"], max_calls=300, max_cost=Decimal("20")
    )
    run.settings = cfg.model_copy(
        update={
            "model_max_output_tokens": original["generation_output_tokens"],
            "model_daily_usd": Decimal("20"),
        }
    )
    data = ROOT / "runtime/reports/chat-skill-gold-v1"
    run.gold, run.hold, run.guards = [
        json.loads((data / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("develop", "confirm", "guards")
    ]
    run.folder, run.report = folder, copy.deepcopy(original)
    run.hashes = {}
    for key, digest in original["source_sha256"].items():
        path = folder / "inputs" / key
        path = path if path.exists() else ROOT / key
        assert sha256(path.read_bytes()).hexdigest() == digest, key
        run.hashes[str(path.relative_to(ROOT))] = digest
    for key in (
        "lisa/chat.py",
        "lisa/dialogue.py",
        "lisa/dialogue_skill.py",
        "lisa/style_distillation.py",
    ):
        assert (ROOT / key).read_bytes() == (folder / "inputs" / key).read_bytes(), key
    run.native, run.uid, run.reused_baseline, run.consecutive_upstream_errors = True, None, None, 0
    run.engine = make_engine(run.settings)
    run.review_text = (
        folder / "inputs/lisa/skills/natural-dialogue/references/review.md"
    ).read_text(encoding="utf-8")
    run.freeze_model_calls(ROOT / original["execution_model_calls"]["path"])
    before = folder / ("before_resume_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json")
    before.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")
    run.report.setdefault("isolated_evaluation_history", []).append(
        run.report.pop("isolated_evaluation")
    )
    run.report["frozen_resume"] = {
        "before": str(before.relative_to(ROOT)),
        "completed_calls_reused": len(original["calls"]),
        "executable_source_hashes": dict(run.hashes),
        "scoring_not_started": True,
    }
    for key in (
        "source_drift",
        "error",
        "stop_reason",
        "finished_at_utc",
        "temporary_account_removed",
    ):
        run.report.pop(key, None)
    run.report.update(status="running", pairs=[], judgments=[])
    generate = run.generate

    async def reuse(self, group, case, rep, variant, prompt):
        key = f"{group}/{case['id']}/r{rep}/{variant}"
        cached = original["generations"].get(key)
        if cached and not cached.get("error"):
            return copy.deepcopy(cached)
        assert not any(c["label"].startswith(key + "/") for c in original["calls"]), key
        return await generate(group, case, rep, variant, prompt)

    async def calibration(self):
        assert self.report["calibration"]["passed"]

    run.generate = MethodType(reuse, run)
    run.calibrate = MethodType(calibration, run)
    await run.execute()
    return run
