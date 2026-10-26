import pytest

from lisa.context_budget import ContextBudgetExceeded, ContextItem, pack_mapping


def test_required_fields_are_kept_and_optional_fields_follow_priority():
    packed = pack_mapping(
        [
            ContextItem("required", "保留", required=True),
            ContextItem("low", "低" * 20, priority=1),
            ContextItem("high", "高", priority=10),
        ],
        35,
    )
    assert packed.values == {"required": "保留", "high": "高"}
    assert packed.included == ("required", "high")
    assert packed.omitted == ("low",)


def test_required_overflow_is_explicit():
    with pytest.raises(ContextBudgetExceeded):
        pack_mapping([ContextItem("required", "超" * 100, required=True)], 10)


def test_custom_measure_can_include_instructions_and_schema():
    packed = pack_mapping(
        [ContextItem("required", "固定", required=True), ContextItem("optional", "可选")],
        7,
        measure=lambda values: len("固定指令") + sum(len(str(v)) for v in values.values()),
    )
    assert packed.values == {"required": "固定"}
    assert packed.omitted == ("optional",)
