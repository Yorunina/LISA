from lisa.style_runtime import (
    CHAT_STYLE_CARD_BYTES,
    WRITING_STYLE_CARD_BYTES,
    compile_runtime_style_card,
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
