"""Best-effort, content-free retrieval observations; never a second message history."""

import logging
from copy import deepcopy
from uuid import UUID

from sqlalchemy.orm import Session

from lisa.models import RetrievalObservation

logger = logging.getLogger(__name__)


def reference_key(reference: dict) -> str:
    field = reference.get("field") or reference.get("start", 0)
    if reference.get("source_kind") == "memory":
        field = "memory"
    return f"{reference['source_kind']}:{reference['source_id']}:{field}"


def context_keys(context, *, public=False) -> list[str]:
    references = [
        ref for ref in context.references if (ref["source_kind"] == "shared") == public
    ]
    keys = [reference_key(ref) for ref in references]
    if not public:
        keys.extend(f"memory:{value}:memory" for value in context.memory_ids)
    mapping = context.metrics.get("reference_key_map", {})
    return list(dict.fromkeys(mapping.get(key, key) for key in keys))


def record_retrieval(engine, context, task_id, user_id, purpose):
    """Only fixed metric fields, IDs and counts reach the observation table."""
    try:
        metrics = deepcopy(context.metrics)
        metrics.setdefault("assembled", len(context_keys(context)))
        metrics.setdefault("assembled_chars", len(context.text) - len(context.public_text))
        metrics.setdefault("final_count", None)
        metrics.setdefault("final_chars", None)
        with Session(engine) as db, db.begin():
            row = RetrievalObservation(
                task_id=task_id, user_id=user_id, purpose=purpose, metrics=metrics
            )
            db.add(row)
            db.flush()
            context.observation_id = row.id
    except Exception:
        # No exception text: SQL parameters can contain private values.
        logger.warning("retrieval_observation_unavailable")
    return context


def finalize_context(engine, context, *, reason=None):
    """Record the final prepared model context, independently of later model adoption."""
    if context.observation_id is None:
        return
    try:
        metrics = deepcopy(context.metrics)
        keys = context_keys(context)
        metrics["final_complete_keys"] = [
            key for key in keys if key not in metrics.get("partial_keys", [])
        ]
        metrics.update(final_count=len(keys), final_keys=keys,
                       final_chars=max(0, len(context.text) - len(context.public_text)))
        public = dict(metrics.get("public", {}))
        public.update(final_count=len(context_keys(context, public=True)),
                      final_chars=len(context.public_text))
        metrics["public"] = public
        if reason:
            omitted = dict(metrics.get("omitted", {}))
            omitted[reason] = omitted.get(reason, 0) + metrics.get("assembled", 0)
            metrics["omitted"] = omitted
        context.metrics = metrics
        with Session(engine) as db, db.begin():
            row = db.get(RetrievalObservation, UUID(str(context.observation_id)))
            if row:
                row.metrics = metrics
    except Exception:
        logger.warning("retrieval_final_observation_unavailable")
