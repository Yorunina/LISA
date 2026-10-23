from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_works import HEADERS
from test_works import owner as owner
from test_works import project as project

from lisa.characters import DEFAULT_CHARACTER_PROMPT, adopt_character_names
from lisa.models import (
    Account,
    Character,
    CharacterParticipation,
    IndexChunk,
    IndexState,
    ModelCall,
    Persona,
    Project,
    Task,
)

CONFIG = {
    "name": "林舟",
    "prompt": "只提出林舟自己的反应。",
    "known_information": "林舟只知道列车晚点，不知道信件内容。",
    "enabled": True,
}
RETRIEVAL_DEFAULTS = {"enabled": False, "limit": 3, "max_chars": 1500}


def create(client, project, **changes):
    response = client.post(
        f"/api/projects/{project['id']}/characters", json=CONFIG | changes, headers=HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()


def task_for(db, owner, project_id, kind="scene"):
    task = Task(user_id=owner, project_id=project_id, kind=kind, status="completed")
    db.add(task)
    db.flush()
    return task


def test_save_revision_and_no_activity_or_model_side_effects(client, db, owner, project):
    account = db.get(Account, owner)
    account.last_active_at = datetime.now(UTC) - timedelta(days=2)
    activity = account.last_active_at
    work = db.get(Project, UUID(project["id"]))
    before = (account.memory_revision, work.index_revision, work.characters)
    db.commit()
    entry = create(client, project, name=" 林舟 ")
    assert entry["name"] == "林舟"
    assert entry["revision"] == 1
    path = f"/api/characters/{entry['id']}"
    assert client.put(path, json=CONFIG, headers=HEADERS).json()["revision"] == 1
    updated = CONFIG | {"known_information": "林舟已经读过信件。"}
    assert client.put(path, json=updated, headers=HEADERS).json()["revision"] == 2
    assert client.get(f"/api/projects/{project['id']}/characters").json()[0] == (
        entry | updated | {"revision": 2}
    )
    db.refresh(account)
    db.refresh(work)
    assert account.last_active_at == activity
    assert (account.memory_revision, work.index_revision, work.characters) == before
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"name": " "},
        {"prompt": " "},
        {"known_information": " "},
        {"retrieval_config": {"limit": 9}},
        {"retrieval_config": {"limit": True}},
        {"retrieval_config": {"sources": ["project"]}},
    ],
)
def test_invalid_configuration_preserves_saved_data(client, project, changes):
    entry = create(client, project)
    invalid = CONFIG | changes
    assert (
        client.put(f"/api/characters/{entry['id']}", json=invalid, headers=HEADERS).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/projects/{project['id']}/characters", json=invalid, headers=HEADERS
        ).status_code
        == 422
    )
    assert client.get(f"/api/projects/{project['id']}/characters").json() == [entry]


def test_disabled_empty_knowledge_and_unique_name_per_work(client, project):
    entry = create(client, project, known_information="", enabled=False)
    assert not entry["enabled"]
    assert (
        client.post(
            f"/api/projects/{project['id']}/characters", json=CONFIG, headers=HEADERS
        ).status_code
        == 409
    )
    second = create(client, project, name="周宁")
    assert (
        client.put(f"/api/characters/{second['id']}", json=CONFIG, headers=HEADERS).status_code
        == 409
    )
    other = client.post(
        "/api/projects", json={"title": "另一部小说", "kind": "novel"}, headers=HEADERS
    ).json()
    assert create(client, other)["name"] == entry["name"]


def test_retrieval_defaults_require_login(client):
    assert client.get("/api/characters/defaults").status_code == 401


def test_scene_and_retrieval_config_save_restore_defaults(client, db, owner, project):
    defaults = client.get("/api/characters/defaults")
    assert defaults.status_code == 200
    assert defaults.json()["retrieval_config"] == RETRIEVAL_DEFAULTS
    assert isinstance(defaults.json()["indexing_enabled"], bool)
    entry = create(client, project)
    assert entry["current_scene"] == ""
    assert entry["retrieval_config"] == RETRIEVAL_DEFAULTS
    role = db.get(Character, UUID(entry["id"]))
    # Existing and automatically created roles use the database's empty JSON default.
    role.retrieval_config = {}
    db.commit()
    path = f"/api/characters/{entry['id']}"
    assert client.put(path, json=CONFIG, headers=HEADERS).json()["revision"] == 1
    custom = CONFIG | {
        "current_scene": " 林舟在站台，能看见关闭的车门。 ",
        "retrieval_config": {"enabled": True, "limit": 8, "max_chars": 3000},
    }
    saved = client.put(path, json=custom, headers=HEADERS).json()
    assert saved["revision"] == 2
    assert saved["current_scene"] == custom["current_scene"].strip()
    assert saved["retrieval_config"] == custom["retrieval_config"]
    assert client.put(path, json=custom, headers=HEADERS).json() == saved
    assert client.get(f"/api/projects/{project['id']}/characters").json() == [saved]
    restored = client.put(path, json=CONFIG, headers=HEADERS).json()
    assert restored["current_scene"] == ""
    assert restored["retrieval_config"] == RETRIEVAL_DEFAULTS
    assert restored["revision"] == 3
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_character_delete_removes_only_its_index_and_chunks(client, db, owner, project):
    entry = create(client, project)
    other = create(client, project, name="周宁")
    states = []
    for item in (entry, other):
        state = IndexState(
            user_id=owner,
            source_kind="character",
            source_id=UUID(item["id"]),
            revision=1,
            space="synthetic",
        )
        db.add(state)
        db.flush()
        db.add(
            IndexChunk(
                state_id=state.id,
                chunk_index=0,
                revision=1,
                space="synthetic",
                start=0,
                end=4,
                embedding=[1.0, 0.0],
            )
        )
        states.append(state.id)
    db.commit()
    assert client.delete(f"/api/characters/{entry['id']}", headers=HEADERS).status_code == 204
    assert list(db.scalars(select(IndexState.id))) == [states[1]]
    assert list(db.scalars(select(IndexChunk.state_id))) == [states[1]]


def test_account_isolation_initialization_and_csrf(client, db, owner, project):
    foreign = Account(username="foreign_character", password_hash="synthetic")
    db.add(foreign)
    db.flush()
    work = Project(user_id=foreign.id, title="秘密作品")
    db.add(work)
    db.flush()
    role = Character(project_id=work.id, **CONFIG)
    db.add(role)
    task = task_for(db, foreign.id, work.id)
    db.commit()
    assert client.get(f"/api/projects/{work.id}/characters").status_code == 404
    assert (
        client.post(f"/api/projects/{work.id}/characters", json=CONFIG, headers=HEADERS).status_code
        == 404
    )
    assert client.put(f"/api/characters/{role.id}", json=CONFIG, headers=HEADERS).status_code == 404
    assert client.delete(f"/api/characters/{role.id}", headers=HEADERS).status_code == 404
    assert client.get(f"/api/tasks/{task.id}/characters").status_code == 404
    assert client.post(f"/api/projects/{project['id']}/characters", json=CONFIG).status_code == 403
    db.delete(db.scalar(select(Persona).where(Persona.user_id == owner)))
    db.commit()
    assert client.get(f"/api/projects/{project['id']}/characters").status_code == 403
    assert (
        client.post(
            f"/api/projects/{project['id']}/characters", json=CONFIG, headers=HEADERS
        ).status_code
        == 403
    )


def test_participations_only_metadata_and_deleted_role_has_no_name(client, db, owner, project):
    entry = create(client, project)
    task = task_for(db, owner, UUID(project["id"]))
    db.add(
        CharacterParticipation(
            task_id=task.id,
            position=0,
            character_id=UUID(entry["id"]),
            revision=1,
            state="completed",
        )
    )
    chat = task_for(db, owner, UUID(project["id"]), "chat")
    db.commit()
    path = f"/api/tasks/{task.id}/characters"
    participation = client.get(path).json()["participations"][0]
    assert participation == {
        "position": 0,
        "character_id": entry["id"],
        "name": "林舟",
        "revision": 1,
        "state": "completed",
        "created_at": participation["created_at"],
    }
    assert client.get(f"/api/tasks/{chat.id}/characters").status_code == 404
    assert client.get(f"/api/tasks/{uuid4()}/characters").status_code == 404
    assert client.delete(f"/api/characters/{entry['id']}", headers=HEADERS).status_code == 204
    assert client.get(path).json()["participations"] == [
        participation | {"name": None, "character_id": None}
    ]
    assert client.get(f"/api/projects/{project['id']}/characters").json() == []
    assert client.delete(f"/api/characters/{entry['id']}", headers=HEADERS).status_code == 404


def test_auto_creation_uses_only_adopted_names_and_preserves_existing(client, db, owner, project):
    entry = create(client, project)
    work = db.get(Project, UUID(project["id"]))
    work.worldbook = "不应泄漏的秘密。"
    work.characters = "作者全局角色资料。"
    task = task_for(db, owner, work.id)
    adopt_character_names(
        db,
        task,
        ["林舟", "周宁", "周宁", "未出场", "沈禾", "第四人"],
        "林舟见到了周宁、沈禾和第四人。",
    )
    db.commit()
    roles = {item.name: item for item in db.scalars(select(Character))}
    assert set(roles) == {"林舟", "周宁", "沈禾"}
    assert roles["林舟"].prompt == CONFIG["prompt"]
    assert roles["林舟"].known_information == CONFIG["known_information"]
    assert roles["林舟"].revision == entry["revision"]
    for name in ("周宁", "沈禾"):
        assert roles[name].prompt == DEFAULT_CHARACTER_PROMPT
        assert roles[name].known_information == ""
        assert not roles[name].enabled
    assert db.scalar(select(func.count()).select_from(Task)) == 1
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_auto_creation_is_transactional_and_project_bound(db, owner, project):
    original = UUID(project["id"])
    other = Project(user_id=owner, title="另一个项目", is_current=False)
    diary = Project(user_id=owner, title="日记", kind="diary", is_current=False)
    db.add_all([other, diary])
    db.flush()
    existing = Character(project_id=original, name="林舟", prompt="已有配置。")
    db.add(existing)
    task = task_for(db, owner, other.id)
    diary_task = task_for(db, owner, diary.id)
    db.commit()
    adopt_character_names(db, task, ["林舟"], "林舟出现。")
    db.flush()
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Character)) == 1
    adopt_character_names(db, diary_task, ["林舟"], "林舟出现。")
    adopt_character_names(db, Task(user_id=uuid4(), project_id=other.id), ["林舟"], "林舟出现。")
    adopt_character_names(db, task, [" ", "长" * 61, "没有出现"], "长" * 61)
    db.flush()
    assert db.scalar(select(func.count()).select_from(Character)) == 1
    adopt_character_names(db, task, ["林舟"], "林舟出现。")
    db.commit()
    roles = db.scalars(select(Character).where(Character.name == "林舟")).all()
    assert {role.project_id for role in roles} == {original, other.id}
    assert db.get(Character, existing.id).prompt == "已有配置。"
