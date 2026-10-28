from pathlib import Path

import pytest

from lisa import dialogue_skill


@pytest.mark.parametrize(
    ("stage", "mode"),
    [("develop-review", "corpus"), ("focus", "v3"), ("confirm", "review"),
     ("native", "review"), ("probe", "review")],
)
def test_retired_research_workflows_fail_before_database_access(monkeypatch, stage, mode):
    from scripts import run_skill_evaluation

    def unexpected_settings():
        pytest.fail("Retired workflow must not read runtime settings or connect to databases")

    monkeypatch.setattr(run_skill_evaluation, "Settings", unexpected_settings)
    monkeypatch.setattr(
        "sys.argv", ["run_skill_evaluation.py", "--stage", stage, "--mode", mode]
    )
    with pytest.raises(SystemExit) as error:
        run_skill_evaluation.main()
    assert error.value.code == 2


def test_fixed_evaluation_clock_preserves_rules_before_timestamp():
    from scripts.benchmark_dialogue_skill_native import fixed_evaluation_clock

    host = "你是LISA。reply为完整回复。不能编造已执行行为。"
    source = host + "当前 UTC 时间：2026-10-01T00:00:00+00:00。\n人格和交流约定"
    frozen = fixed_evaluation_clock(source, "2026-10-28T00:00:00+00:00")
    assert frozen == host + "当前 UTC 时间：2026-10-28T00:00:00+00:00。\n人格和交流约定"


def test_external_compilation_is_self_contained_and_does_not_include_discovery_metadata():
    core = dialogue_skill.compile_dialogue_skill("core")
    examples = dialogue_skill.compile_dialogue_skill("examples")
    assert "references/examples.md" not in core + examples
    assert "references/review.md" not in core + examples
    assert "## 宿主执行" not in core + examples
    assert "name: natural-dialogue" not in core + examples
    assert "对方：新买的笔" not in core
    assert "对方：新买的笔" in examples
    assert dialogue_skill.dialogue_skill_fingerprint("core") != (
        dialogue_skill.dialogue_skill_fingerprint("examples")
    )


@pytest.mark.parametrize("mode", ["arbitrary-file", "review"])
def test_invalid_mode_fails_before_loading_files(mode):
    with pytest.raises(ValueError, match="Unknown"):
        dialogue_skill.compile_dialogue_skill(mode)


def test_private_corpus_is_loaded_only_when_explicitly_selected(monkeypatch):
    read = Path.read_text

    def resource(path, *args, **kwargs):
        if path.name == "corpus.md":
            return "来话：合成测试问题\n真人接话：仅供测试的回应选择"
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", resource)
    assert "仅供测试的回应选择" in dialogue_skill.compile_dialogue_skill("corpus")
    assert "仅供测试的回应选择" not in dialogue_skill.compile_dialogue_skill("core")


def test_oversize_resource_fails_without_silently_truncating_constraints(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: "---\nname: test\n---\n" + "中" * 5000)
    with pytest.raises(ValueError, match="budget"):
        dialogue_skill.compile_dialogue_skill()


def test_chat_guide_is_opt_in_and_frozen_for_retries(db, settings, monkeypatch):
    from sqlalchemy import select

    from lisa.chat import _chat_instructions
    from lisa.chat_style import expression_guide
    from lisa.models import Account, Persona, Task

    user = Account(username="skill_fixture", password_hash="unused")
    db.add(user)
    db.flush()
    persona = Persona(user_id=user.id, name="测试", prompt="合成的人格资料", version=1)
    task = Task(user_id=user.id, kind="chat", source="development", status="running", request={})
    db.add_all([persona, task])
    db.flush()
    assert type(settings).model_fields["dialogue_skill_mode"].default == "off"
    default = _chat_instructions(db, user.id, task, persona, ["今天很累"])
    assert expression_guide() in default
    assert "本 skill 不补造背景" not in default

    other = Task(user_id=user.id, kind="chat", source="development", status="running", request={})
    db.add(other)
    db.flush()
    active = _chat_instructions(
        db, user.id, other, persona, ["今天很累"], dialogue_skill_mode="examples"
    )
    assert dialogue_skill.compile_dialogue_skill("examples") in active
    assert expression_guide() not in active
    assert "合成的人格资料" in active
    assert "memories" in active and "life_events" in active and "message_intervals" in active
    frozen = dict(other.input_snapshot["dialogue_guide"])
    db.commit()
    db.expire_all()
    other = db.scalar(select(Task).where(Task.id == other.id))
    monkeypatch.setattr(dialogue_skill, "compile_dialogue_skill", lambda *a, **kw: "修改后的指引")
    retried = _chat_instructions(
        db, user.id, other, persona, ["今天很累"], dialogue_skill_mode="core"
    )
    assert frozen["text"] in retried
    assert "修改后的指引" not in retried
    assert other.input_snapshot["dialogue_guide"] == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_review", [False, True])
async def test_shared_skill_uses_one_generation_and_rejects_legacy_review(
    db, engine, settings, monkeypatch, legacy_review
):
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from lisa.chat import execute_chat, ingest_message
    from lisa.dialogue import Conversation
    from lisa.models import Account, ChatMessage, OutboundMessage, Persona, QQBinding, Task

    monkeypatch.setattr(dialogue_skill, "compile_dialogue_skill", lambda mode: "合成交流约束")
    user = Account(username="review_fixture", password_hash="unused")
    db.add(user)
    db.flush()
    db.add_all(
        [
            QQBinding(user_id=user.id, qq_id="712345678"),
            Persona(user_id=user.id, name="合成", prompt="合成人格", version=1),
        ]
    )
    db.commit()
    task_id = ingest_message(
        db,
        bot_id="912345678",
        message_id="review_1",
        qq_id="712345678",
        content="请给我一句话",
        sent_at=datetime.now(UTC),
    )
    db.commit()
    if legacy_review:
        task = db.get(Task, task_id)
        task.input_snapshot = {"dialogue_guide": {
            "mode": "review", "text": "旧快照", "review_text": "旧复核"
        }}
        db.commit()
    calls = []

    async def generate(*args):
        calls.append(args)
        assert "合成交流约束" in args[4]
        return Conversation(reply="单次自然回复", messages=["单次自然回复"])

    monkeypatch.setattr("lisa.chat.generate_conversation", generate)
    await execute_chat(
        settings.model_copy(update={"dialogue_skill_mode": "core"}), engine, str(task_id)
    )
    db.expire_all()
    assert len(calls) == (0 if legacy_review else 1)
    assert db.get(Task, task_id).status == ("failed" if legacy_review else "completed")
    outputs = list(db.scalars(select(OutboundMessage.content)))
    if legacy_review:
        assert db.get(Task, task_id).error == "model_failed"
        assert all("旧复核" not in value and "旧快照" not in value for value in outputs)
    else:
        assert outputs == ["单次自然回复"]
        assert "review_text" not in db.get(Task, task_id).input_snapshot["dialogue_guide"]
    assert db.scalar(select(func.count()).select_from(ChatMessage)) == 0
