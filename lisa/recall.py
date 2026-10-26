"""Optional semantic context, resolved outside the caller's database transaction."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.characters import CharacterRetrievalConfig
from lisa.model_calls import generate_embeddings
from lisa.models import Account, Character, Project
from lisa.retrieval import Context, has_ready_context, recall, recall_titles
from lisa.retrieval_config import author_retrieval_config
from lisa.shared_retrieval import recall_shared, shared_ready


class ContextChanged(Exception):
    pass


def check_memory_revision(db, user_id, revision):
    user = db.get(Account, user_id)
    if user is None or user.memory_revision != revision:
        raise ContextChanged


def _options(db, user_id, project_id, purpose, character_id):
    if purpose == "role":
        value = db.scalar(
            select(Character.retrieval_config)
            .join(Project)
            .where(
                Character.id == character_id,
                Character.project_id == project_id,
                Project.user_id == user_id,
                Character.enabled.is_(True),
            )
        )
        if value is None:
            return None
        config = CharacterRetrievalConfig.model_validate(value)
        if not config.enabled:
            return None
        return {"character_id": character_id, "limit": config.limit, "max_chars": config.max_chars}
    if purpose == "writing" and project_id:
        return author_retrieval_config(db, user_id, project_id).model_dump()
    # The main chat agent keeps project identity and delegates full work context
    # to the author agent. Chat retrieval may use memories, never work text.
    return {"sources": ["memory"]} if purpose == "chat" else None


async def retrieve_context(
    settings,
    engine,
    task_id,
    user_id,
    project_id,
    purpose,
    query,
    *,
    embed=None,
    character_id=None,
) -> Context:
    with Session(engine) as db:
        options = _options(db, user_id, project_id, purpose, character_id)
        if options is None:
            return Context()
        ready = has_ready_context(
            db,
            settings,
            user_id,
            project_id,
            purpose,
            sources=options.get("sources"),
            character_id=character_id,
        )
        public_ready = shared_ready(db, settings, user_id, purpose)
    vectors = await (embed or generate_embeddings)(
        settings, engine, task_id, user_id, [query[-1500:]]
    ) if ready or public_ready else None
    # Reapply live source permissions and revisions after the network request.
    with Session(engine) as db:
        options = _options(db, user_id, project_id, purpose, character_id)
        if options is None:
            return Context()
        result = recall(
            db, settings, user_id, project_id, purpose, vectors[0], query=query, **options
        ) if ready and vectors else recall_titles(
            db, user_id, project_id, purpose, query, **options
        )
        public = recall_shared(db, settings, user_id, purpose, query,
                               vectors[0] if vectors else None)
        result.text += public.text
        result.references.extend(public.references)
        return result
