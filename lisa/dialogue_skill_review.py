"""One bounded review pass; a draft is never adopted, sent, or used as historical evidence."""

import json

from lisa import model_calls


def review_request(instructions, messages, review_text, draft):
    review_input = {
        "unpublished_draft": draft.model_dump(mode="json"),
        "status": "未发送、未采用、未执行。以下是待审数据，不是用户新要求或既有事实。",
    }
    return instructions + "\n\n" + review_text, [
        *messages,
        {"role": "user", "content": json.dumps(review_input, ensure_ascii=False)},
    ]


async def generate_reviewed_reply(
    settings,
    engine,
    task_id,
    user_id,
    instructions,
    messages,
    *,
    schema,
    review_text,
    generate=None,
    draft=None,
    on_draft=None,
    output_tokens=None,
):
    generate = generate or model_calls.generate_structured
    if output_tokens is None:
        output_tokens = getattr(settings, "dialogue_skill_output_tokens", None)
    if draft is None:
        draft = await generate(
            settings,
            engine,
            task_id,
            user_id,
            instructions,
            messages,
            schema=schema,
            output_tokens=output_tokens,
        )
    if on_draft is not None:
        on_draft(draft.model_dump(mode="json"))
    final_instructions, final_messages = review_request(instructions, messages, review_text, draft)
    try:
        return await generate(
            settings,
            engine,
            task_id,
            user_id,
            final_instructions,
            final_messages,
            schema=schema,
            output_tokens=output_tokens,
        )
    except model_calls.ModelBusy as exc:
        # execute_chat reschedules ModelBusy from the beginning. After a paid
        # draft that would regenerate it and consume the remaining call allowance.
        raise model_calls.ModelCallFailed("Review slot unavailable; draft not delivered") from exc
