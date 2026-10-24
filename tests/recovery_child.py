"""Subprocess fixture: real worker/gateway, synthetic providers, disposable DB only."""

import asyncio
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa import author, chat  # noqa: E402
from lisa.gateway import run_gateway  # noqa: E402
from lisa.model_calls import _finish, _reserve  # noqa: E402
from lisa.settings import Settings  # noqa: E402
from lisa.worker import run_worker  # noqa: E402

if not re.fullmatch(r"lisa_test_[0-9a-f]{32}", os.environ.get("LISA_DB_NAME", "")):
    raise SystemExit("Disposable database required")

mode = sys.argv[1]


async def generate(settings, engine, task_id, user_id, *_args):
    if mode == "before_call":
        await asyncio.Future()
    call_id = _reserve(settings, engine, task_id, user_id, Decimal("0.01"))
    if mode == "inflight":
        await asyncio.Future()
    _finish(engine, call_id, state="succeeded", charged=Decimal("0.005"))
    if mode == "settled":
        await asyncio.Future()
    return {
        "title": "Recovered scene",
        "paragraphs": ["A letter arrived in the rain."],
        "summary": "A letter arrived.",
        "plot_state": "The letter is unopened.",
    }


async def conversation(*args):
    await generate(*args)
    return {"reply": "Synthetic recovered conversation.", "opinions": [], "commission": None}


author.generate_writing = generate
chat.generate_conversation = conversation
chat.generate_dialogue = conversation

if mode == "adopted":
    original = author.execute_author

    async def after_adoption(*args):
        await original(*args)
        await asyncio.Future()

    author.execute_author = after_adoption

factory = asyncio.SelectorEventLoop if os.name == "nt" else None
with asyncio.Runner(loop_factory=factory) as runner:
    coroutine = (
        run_gateway(Settings()) if mode == "gateway" else run_worker(once=mode == "complete")
    )
    runner.run(coroutine)
