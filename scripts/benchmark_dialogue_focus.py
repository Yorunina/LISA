"""Paired regression for conversational focus versus explicit task completeness."""

import json
import random
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete
from sqlalchemy.orm import Session

from lisa.dialogue_skill import compile_dialogue_skill, review_guide
from lisa.models import Account
from scripts.benchmark_chat_distillation import Judgments, checked_judgments
from scripts.benchmark_chat_prompt import group_summary, scored_rows, sha
from scripts.benchmark_dialogue_skill import DATA, IDENTITY, ROOT, SKILL_ROOT
from scripts.benchmark_dialogue_skill_native import NativeSkillRun

FIXTURE = ROOT / "tests/fixtures/dialogue_focus_v2.json"
BASELINE = ROOT / "runtime/reports/dialogue-focus-v2/v1.json"
JUDGE = """你评审中文对话的两个匿名回复，输出指定Judgments JSON。
本量表按当前用户明确表达的聊天偏好评审，不声称代表所有人的自然度判断。
用户指出：对“不想再A，先B”的普通收尾，回复“行，不A了，先B”属于逐项复述确认，
比“好吧”“那先B吧”等承接更有非人感。这是本轮已确认的评分锚点，不可仍把两者都打4分。
如果回复只是重复来话多个分句再认同，既无必要核对也无新的反应，应体现自然度扣分；
不要把相关信息之间自然的玩笑、必要回应或明确多项交办错误归为这种缺陷。
自然度0—4：看是否接住当前主要言语意图、措辞顺口、回应分寸合适。
闲聊允许次要信息留白。不要因为逐项覆盖来话、复述后认同、同时确认每个分句而加分。
转场或收尾时简单承接可以完整完成对话动作；不要固定只回末句，也不要给短答自动加分。
明确交办、核对、多项求助必须按要求完成；短但遗漏任务不算自然可用。
事实新增、主体混淆、无依据肯定或否定单列fabrication，不拿幽默比喻当字面事实。
rubric_met同时要求接住主要来意、完成明确交办且无虚构；不要求覆盖闲聊全部信息。
unasked_advice标无请求指导，question_pressure标多余追问，counseling标咨询/训导腔。
personality评有具体反应的平等口吻。reason给简要具体依据；winner为a/b/tie/uncertain。
不得因候选顺序、字数、参考句式或是否用了某个词判优。只用已给上下文。
输入cases包含case_id、context、expected、a和b；逐题返回全部评分字段。"""


async def execute(version="v2"):
    if version not in {"v2", "v3"}:
        raise ValueError("Unknown focus candidate")
    fixture = FIXTURE.with_name(f"dialogue_focus_{version}.json")
    args = SimpleNamespace(
        stage="develop",
        modes=["review"],
        max_calls=90,
        max_cost=Decimal("5"),
        model=None,
        reuse_baseline=None,
        prompt=SKILL_ROOT / "SKILL.md",
    )
    datasets = [
        json.loads((DATA / f"{n}.json").read_text(encoding="utf-8"))
        for n in ("develop", "confirm", "guards")
    ]
    run = NativeSkillRun(args, *datasets)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    guides = {"v1": baseline["guide"], version: compile_dialogue_skill("review")}
    reviews = {"v1": baseline["review"], version: review_guide()}
    run.report.update(
        experiment="dialogue_focus_" + version,
        validation_scope=(
            "Human-feedback targeted synthetic regression; not general confirmation."
        ),
        prompt_hashes={"review": sha((IDENTITY + guides[version]).encode())},
        variants={
            k: {"guide_sha256": sha(v.encode()), "review_sha256": sha(reviews[k].encode())}
            for k, v in guides.items()
        },
        focus_cases=cases,
    )
    for path in (Path(__file__).resolve(), fixture, BASELINE, ROOT / "lisa/structured_output.py"):
        key = str(path.relative_to(ROOT))
        run.hashes[key] = sha(path.read_bytes())
        target = run.folder / "inputs" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    run.report["source_sha256"].update(run.hashes)
    with Session(run.engine) as db, db.begin():
        user = Account(username="skillfocus_" + run.folder.name[-6:], password_hash="!")
        db.add(user)
        db.flush()
        run.uid = user.id
    try:
        calibration = [
            {
                "case_id": "close",
                "context": ["不想再复盘，我去吃饭。"],
                "expected": "简单收尾，不需逐项确认。",
                "a": ["好，去吧。"],
                "b": ["行，不复盘了，先去吃饭。"],
            },
            {
                "case_id": "task",
                "context": ["确认姐姐、周五三点、东门。"],
                "expected": "明确核对三项。",
                "a": ["姐姐，周五下午三点，东门。"],
                "b": ["好。"],
            },
        ]
        value = await run.call(
            "judge/calibration",
            JUDGE,
            [{"role": "user", "content": json.dumps({"cases": calibration}, ensure_ascii=False)}],
            Judgments,
        )
        scores = checked_judgments(value, [c["case_id"] for c in calibration])
        run.report["calibration"] = {
            "scores": scores,
            "passed": all(s["winner"] == "a" for s in scores),
        }
        run.save()
        if not run.report["calibration"]["passed"]:
            raise RuntimeError("focus_calibration_failed")
        for case in random.Random(20261028).sample(cases, len(cases)):
            outputs = {}
            for arm in random.Random(case["id"]).sample(["v1", version], 2):
                run.review_text = reviews[arm]
                outputs[arm] = await run.generate(arm, case, 0, "review", IDENTITY + guides[arm])
            run.report["pairs"].append(
                {
                    "id": case["id"],
                    "group": case["category"],
                    "case": case,
                    "rep": 0,
                    "variant": "candidate",
                    "outputs": {"baseline": outputs["v1"], "candidate": outputs[version]},
                }
            )
            run.save()
            if run.report.get("source_drift") or run.report.get("upstream_circuit_open"):
                raise RuntimeError("execution_guard")
        valid = [
            p for p in run.report["pairs"] if not any(v.get("error") for v in p["outputs"].values())
        ]
        for role in ("primary", "reversed"):
            for pair in random.Random(2048).sample(valid, len(valid)):
                order = random.Random(pair["id"]).sample(["baseline", "candidate"], 2)
                if role == "reversed":
                    order.reverse()
                mapping = dict(zip(("a", "b"), order))
                payload = {
                    "case_id": pair["id"],
                    "context": pair["case"]["turns"],
                    "expected": pair["case"]["expected"],
                    **{k: pair["outputs"][v]["turns"] for k, v in mapping.items()},
                }
                value = await run.call(
                    "judge/" + role + "/" + pair["id"],
                    JUDGE,
                    [
                        {
                            "role": "user",
                            "content": json.dumps({"cases": [payload]}, ensure_ascii=False),
                        }
                    ],
                    Judgments,
                )
                run.report["judgments"].append(
                    {
                        "role": role,
                        "mapping": {pair["id"]: mapping},
                        "scores": checked_judgments(value, [pair["id"]]),
                    }
                )
                run.save()
        run.report["status"] = "completed" if len(valid) == len(cases) else "partial"
    except Exception as exc:
        run.report.update(status="partial", error=type(exc).__name__, stop_reason=str(exc)[:120])
    finally:
        with Session(run.engine) as db, db.begin():
            db.execute(delete(Account).where(Account.id == run.uid))
        rows = scored_rows(run.report)
        run.report["summary"] = {
            "criteria_met": False,
            "scope": "targeted only",
            "groups": {
                g: group_summary([r for r in rows if r["group"] == g])
                for g in sorted({c["category"] for c in cases})
            },
            "calls": len(run.report["calls"]),
            "estimated_cny": str(sum(Decimal(c["cost"]) for c in run.report["calls"])),
        }
        run.report.update(
            finished_at_utc=datetime.now(UTC).isoformat(), temporary_account_removed=True
        )
        run.save()
        run.engine.dispose()
        print(
            json.dumps(
                {
                    "folder": str(run.folder),
                    "status": run.report["status"],
                    "summary": run.report["summary"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return run
