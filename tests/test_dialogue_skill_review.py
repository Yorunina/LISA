from types import SimpleNamespace

import pytest

from lisa.dialogue import Conversation
from lisa.dialogue_skill_review import generate_reviewed_reply
from lisa.model_calls import ModelBusy, ModelCallFailed


@pytest.mark.asyncio
async def test_busy_review_does_not_reschedule_a_paid_draft_from_the_beginning():
    calls = 0

    async def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ModelBusy("occupied")
        return Conversation(reply="未采用草稿")

    with pytest.raises(ModelCallFailed, match="Review slot unavailable"):
        await generate_reviewed_reply(
            None,
            None,
            "task",
            "user",
            "指引",
            [],
            schema=Conversation,
            review_text="复核",
            generate=generate,
        )
    assert calls == 2


@pytest.mark.asyncio
async def test_review_uses_configured_structured_allowance_for_both_paid_calls():
    allowances = []

    async def generate(*args, **kwargs):
        allowances.append(kwargs["output_tokens"])
        return Conversation(reply="完整回应")

    await generate_reviewed_reply(
        SimpleNamespace(dialogue_skill_output_tokens=2400),
        None,
        "task",
        "user",
        "指引",
        [],
        schema=Conversation,
        review_text="复核",
        generate=generate,
    )
    assert allowances == [2400, 2400]


@pytest.mark.asyncio
async def test_review_returns_only_final_decision_and_keeps_actual_input_unchanged():
    original = [{"role": "user", "content": "请改写这句话"}]
    draft = Conversation(reply="这次更新后出了问题", messages=["这次更新后出了问题"])
    final = Conversation(reply="麻烦协助排查问题", messages=["麻烦协助排查问题"])
    calls = []

    async def generate(*args, **kwargs):
        calls.append((args, kwargs))
        return draft if len(calls) == 1 else final

    value = await generate_reviewed_reply(
        None,
        None,
        "same-task",
        "same-user",
        "原始约束",
        original,
        schema=Conversation,
        review_text="检查新增事实",
        generate=generate,
    )
    assert value is final and value is not draft
    assert len(calls) == 2
    assert calls[0][0][2:4] == calls[1][0][2:4] == ("same-task", "same-user")
    assert calls[1][0][5][:-1] == original
    assert "unpublished_draft" in calls[1][0][5][-1]["content"]
    assert original == [{"role": "user", "content": "请改写这句话"}]


@pytest.mark.asyncio
async def test_failed_review_does_not_fall_back_to_an_unchecked_draft():
    async def fail(*args, **kwargs):
        raise ModelCallFailed("failed review")

    with pytest.raises(ModelCallFailed):
        await generate_reviewed_reply(
            None,
            None,
            "same-task",
            "same-user",
            "原始约束",
            [],
            schema=Conversation,
            review_text="检查",
            generate=fail,
            draft=Conversation(reply="尚未核对的草稿"),
        )
