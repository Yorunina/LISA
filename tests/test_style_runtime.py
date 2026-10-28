from lisa.style_runtime import (
    CHAT_STYLE_CARD_BYTES,
    WRITING_STYLE_CARD_BYTES,
    chat_scene,
    compile_runtime_style_card,
    runtime_style_prompt,
    utf8_bytes,
)


def test_chat_card_selects_core_and_current_scene_with_byte_cap():
    report = {
        "profile": {
            "rules": [
                {
                    "feature": "全局",
                    "condition": "信息足够时",
                    "response_choice": "回应重点后停下",
                    "counterexample": "信息不足时保持未知",
                    "scene": "other",
                },
                {
                    "feature": "询问",
                    "condition": "用户明确提问时",
                    "response_choice": "先直接回答",
                    "counterexample": "问题含义不明时先澄清",
                    "scene": "question",
                },
                {
                    "feature": "闲聊",
                    "condition": "普通闲聊时",
                    "response_choice": "具体接住当前话题",
                    "counterexample": "没有足够信息时不补写经历",
                    "scene": "casual",
                },
            ]
        }
    }
    card = compile_runtime_style_card(report, "personality", scene="question")
    assert "先直接回答" in card
    assert "具体接住当前话题" not in card
    assert utf8_bytes(card) <= CHAT_STYLE_CARD_BYTES


def test_writing_card_uses_task_rules_and_never_serializes_report():
    report = {
        "profile": {
            "task_rules": {
                "rewrite": [
                    {
                        "condition": "局部改写时",
                        "write_choice": "保持范围外正文不变",
                        "counterexample": "不要续写整章",
                        "task": "rewrite",
                    }
                ]
            },
            "coverage": {"source_text_that_must_not_enter_runtime": "secret"},
        }
    }
    card = compile_runtime_style_card(report, "writing", task="rewrite")
    assert "保持范围外正文不变" in card
    assert "secret" not in card
    assert utf8_bytes(card) <= WRITING_STYLE_CARD_BYTES


def test_chat_does_not_promote_unrelated_scene_when_core_is_missing():
    report = {"profile": {"rules": [{
        "condition": "告别时", "response_choice": "说再见即可", "scene": "closing",
    }]}}
    card = compile_runtime_style_card(report, "personality", scene="question")
    assert "说再见即可" not in card
    assert "说再见即可" in compile_runtime_style_card(report, "personality")


def test_scene_exception_survives_saturated_core_and_rules_are_whole():
    core = [{
        "condition": "一般交流", "response_choice": f"全局{i}" + "甲" * 230,
        "counterexample": "没有明确要求时不展开", "scene": "other",
    } for i in range(5)]
    current = {
        "condition": "明确求助时", "response_choice": "具体解释处理步骤",
        "counterexample": "只是分享时不提供方案", "scene": "question",
    }
    card = compile_runtime_style_card({"rules": [*core, current]}, "personality", scene="question")
    assert "具体解释处理步骤" in card and "只是分享时不提供方案" in card
    assert utf8_bytes(card) <= CHAT_STYLE_CARD_BYTES
    assert all(line.endswith("时不展开") for line in card.splitlines()[2:])


def test_oversized_rule_is_not_cut_and_does_not_block_later_complete_rule():
    report = {"rules": [
        {"write_choice": "巨长规则" * 2000, "task": "novel"},
        {"feature": "视角限于人物可见事物", "counterexample": "不进入他人内心", "task": "novel"},
        {"write_choice": "这是日记专用", "task": "diary"},
    ]}
    card = compile_runtime_style_card(report, "writing", task="novel")
    assert "巨长规则" not in card
    assert "视角限于人物可见事物" in card and card.endswith("不进入他人内心")
    assert "这是日记专用" not in card


def test_handwritten_and_legacy_prompts_keep_authority():
    prompt = "这是用户手写的条件，不要用旧档案覆盖。"
    report = {"profile": {"rules": [{"response_choice": "旧档案规则", "scene": "other"}]}}
    edits = ({"edited_from_source": True}, {"validation": {"status": "edited_not_evaluated"}})
    for edited in edits:
        assert runtime_style_prompt({**report, **edited}, "personality", prompt) == prompt
    assert runtime_style_prompt({}, "personality", prompt, scene="question") == prompt


def test_small_chat_contrast_keeps_different_choices_for_different_conditions():
    rules = [
        {"scene": "casual", "condition": "普通分享", "response_choice": "回应眼前具体事后停下"},
        {"scene": "disagreement", "condition": "审美分歧", "response_choice": "说明自己的判断"},
        {"scene": "question", "condition": "明确求助", "response_choice": "解释可行步骤"},
        {"scene": "humor", "condition": "轻松玩笑", "response_choice": "可以轻轻打趣",
         "counterexample": "转为认真时停止玩笑"},
    ]
    for rule in rules:
        card = compile_runtime_style_card({"rules": rules}, "personality", scene=rule["scene"])
        assert rule["response_choice"] in card
        assert all(other["response_choice"] not in card for other in rules if other is not rule)


def test_explicit_help_is_not_lost_behind_worry_or_joking():
    assert chat_scene("我很担心明天交不了稿，能帮我看看先写哪一段吗？") == "question"
    assert chat_scene("哈哈，这个结尾难写，请帮我看看问题在哪。") == "question"
    assert chat_scene("烦死了，能不能帮我把这些线索理清楚？") == "question"
    assert chat_scene("这些线索顺序乱了。帮我理一下先后。") == "question"
    assert chat_scene("今天有点烦，不想分析，也不要帮我出主意。") == "boundary"
    assert chat_scene("他今天可以帮我带饭，哈哈。") == "humor"


def test_delayed_transport_does_not_select_a_closing_style_card():
    report = {"rules": [
        {"scene": "question", "response_choice": "认真回答具体问题"},
        {"scene": "closing", "response_choice": "自然结束这轮交流"},
    ]}
    for text in ("晚点的火车怎么退票？", "火车晚点了，怎么改签？"):
        scene = chat_scene(text)
        card = compile_runtime_style_card(report, "personality", scene=scene)
        assert scene == "question"
        assert "认真回答具体问题" in card
        assert "自然结束这轮交流" not in card
    for text in ("晚点", "晚点吧。", "晚点再聊", "我先忙，晚点说。", "晚点见"):
        assert chat_scene(text) == "closing"


def test_supported_core_survives_unobserved_scene_without_borrowing_counsel():
    profile = {"rules": [
        {"scene": "other", "condition": "日常交流", "response_choice": "只接眼前这件事"},
        {"scene": "support", "condition": "对方明确求建议", "response_choice": "说明可行办法"},
    ]}
    card = compile_runtime_style_card(profile, "personality", scene="casual")
    assert "只接眼前这件事" in card
    assert "说明可行办法" not in card
