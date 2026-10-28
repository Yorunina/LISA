"""Optional semantic context, resolved outside the caller's database transaction."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.characters import CharacterRetrievalConfig
from lisa.memory import memory_context
from lisa.model_calls import EmbeddingUnavailable, ModelCallFailed, generate_embeddings
from lisa.models import Account, Character, ModelCall, Project, Task
from lisa.retrieval import (
    Context,
    _source,
    _valid_project,
    has_ready_context,
    recall,
    recall_titles,
)
from lisa.retrieval_config import author_retrieval_config
from lisa.shared_retrieval import recall_shared, shared_ready


class ContextChanged(Exception):
    pass


def check_memory_revision(db, user_id, revision):
    user = db.get(Account, user_id)
    if user is None or user.memory_revision != revision:
        raise ContextChanged


def _options(db, user_id, project_id, purpose, character_id):
    user = db.get(Account, user_id)
    if user is None or not user.active or not _valid_project(db, user_id, project_id):
        return None
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
        # Public entries are currently memes, which are offered only in chat.
        public_ready = purpose == "chat" and shared_ready(db, settings, user_id, purpose)
        # A busy generation may reschedule the same task after query fallback.
        # Remember that exact settled query in its ledger, not its source text.
        used_fallback = purpose in {"chat", "writing"} and db.scalar(
            select(ModelCall.id).where(
                ModelCall.task_id == task_id,
                ModelCall.user_id == user_id,
                ModelCall.state == "failed",
                ModelCall.configuration["query_fallback"].as_boolean().is_(True),
            ).limit(1)
        ) is not None
    try:
        vectors = await (embed or generate_embeddings)(
            settings, engine, task_id, user_id, [query[-1500:]]
        ) if (ready or public_ready) and not used_fallback else None
    except EmbeddingUnavailable as exc:
        if purpose not in {"chat", "writing"}:
            raise
        with Session(engine) as db, db.begin():
            task = db.get(Task, task_id)
            user = db.get(Account, user_id)
            if (
                task is None or task.user_id != user_id or task.status != "running"
                or user is None or not user.active
            ):
                raise ModelCallFailed("Task or account unavailable") from None
            call = db.get(ModelCall, exc.call_id) if exc.call_id is not None else None
            if (
                call is None or call.task_id != task_id or call.user_id != user_id
                or call.state != "failed" or call.configuration.get("api") != "embeddings"
            ):
                raise ModelCallFailed("Settled query call unavailable") from None
            call.configuration = {**call.configuration, "query_fallback": True}
        # Only this query is optional. Re-read sources below; subsequent model
        # calls still reserve against the same task, including this failed call.
        vectors = None
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
        # Choose private context before appending public suggestions. Public-only
        # results must not suppress recent memories, and roles never get this fallback.
        if not result.text and "memory" in options.get("sources", []):
            result.text = memory_context(
                db, user_id, project_id, purpose=purpose,
                max_chars=min(1200, options.get("max_chars", 1200)),
                references=result.memory_ids,
                limit=options.get("limit"),
            )
            if purpose == "writing":
                for memory_id in result.memory_ids:
                    source = _source(db, "memory", memory_id, user_id, project_id, purpose)
                    if source is not None:
                        result.references.append({
                            "source_kind": "memory",
                            "source_id": str(source.id),
                            "revision": source.revision,
                            "space": "recent-text",
                            "chunk_index": 0,
                            "start": 0,
                            "end": len(source.text),
                        })
        public = recall_shared(db, settings, user_id, purpose, query,
                               vectors[0] if vectors else None)
        result.public_text = public.text
        result.text += public.text
        result.references.extend(public.references)
        return result
