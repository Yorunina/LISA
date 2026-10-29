import argparse
import asyncio
import logging
import os

from lisa.queue import create_queue
from lisa.settings import Settings


async def run_worker(*, once: bool = False) -> None:
    from lisa.db import make_engine
    from lisa.recovery import recover_tasks

    settings = Settings()
    engine = make_engine(settings)
    from lisa.runtime_status import process_pulse

    try:
        await asyncio.to_thread(recover_tasks, engine)
        app = create_queue(settings)
        async with process_pulse(engine, settings, ["worker", "image_worker"]):
            async with app.open_async():
                # Each consumer keeps the original concurrency and queue ownership.
                async with asyncio.TaskGroup() as group:
                    for queue, concurrency in (
                        ("lisa", 2),
                        ("lisa_author", 1),
                        ("lisa_index", 1),
                        ("lisa_style", 1),
                        ("lisa_image", 1),
                    ):
                        group.create_task(
                            app.run_worker_async(
                                queues=[queue],
                                concurrency=concurrency,
                                wait=not once,
                                install_signal_handlers=False,
                            )
                        )
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="LISA persistent worker")
    parser.add_argument("--once", action="store_true", help="Exit when the current queue is empty")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(run_worker(once=args.once))


if __name__ == "__main__":
    main()
