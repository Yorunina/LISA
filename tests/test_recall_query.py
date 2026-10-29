import pytest

from lisa.chat_context import recall_query


def message(role, content, *, time="2026-10-29T09:00:00+08:00"):
    return {"role": role, "content": content, "time": time}


@pytest.mark.parametrize("current", [
    "那里我还挺想去的", "那边怎么样", "刚才那个还记得吗", "之前说的地方呢",
])
def test_explicit_references_use_nearby_messages(current):
    history = [message("user", "以前在青岛住过"), message("assistant", "青岛的海风很舒服")]
    query = recall_query(current, history)
    assert query.startswith(current)
    assert "以前在青岛住过" in query
    assert "青岛的海风很舒服" in query
    assert "近期user 时间 2026-10-29T09:00:00+08:00" in query
    assert "近期assistant" in query and "仅指代线索" in query


@pytest.mark.parametrize("current", [
    "这个报错怎么处理", "今天晚饭吃什么", "换个话题，那里怎么报错了", "说点别的，那里有个报错",
])
def test_self_contained_or_explicit_new_topics_keep_current_only(current):
    assert recall_query(current, [message("user", "青岛的旅行计划")]) == current


def test_reference_query_uses_only_two_most_recent_messages():
    query = recall_query("那里怎么样", [
        message("user", "更早的话题"), message("user", "我在青岛"),
        message("assistant", "青岛靠海"),
    ])
    assert "更早的话题" not in query
    assert "我在青岛" in query and "青岛靠海" in query


def test_query_history_budget_includes_role_and_time_labels():
    current = "那里怎么样"
    query = recall_query(current, [message("user", "青岛" * 400), message("assistant", "海风")])
    assert query.startswith(current)
    assert len(query) - len(current) == 400


def test_long_assistant_reply_does_not_displace_the_user_reference():
    current = "那里怎么样"
    query = recall_query(current, [
        message("user", "我小时候在青岛住过"),
        message("assistant", "以前的一些生活细节。" * 100),
    ])
    assert "青岛" in query
    assert "近期assistant" in query and "近期user" in query
    assert len(query) - len(current) <= 400


def test_current_query_wins_when_history_has_no_remaining_room():
    current = "新内容" * 500 + "那里怎么样"
    assert recall_query(current, [message("user", "旧线索")]) == current[-1500:]


def test_total_query_budget_preserves_current_before_history():
    current = "当前内容" * 350 + "那里怎么样"
    query = recall_query(current, [message("user", "青岛" * 200)])
    assert query.startswith(current)
    assert len(query) == 1500
    assert "青岛" in query


def test_absent_history_does_not_change_followup():
    assert recall_query("那里怎么样", []) == "那里怎么样"
