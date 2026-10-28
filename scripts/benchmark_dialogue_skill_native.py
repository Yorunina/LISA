"""Frozen candidate transfer check using LISA's actual structured conversation contract."""

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from lisa.chat import _chat_instructions, _message_parts
from lisa.dialogue import Conversation
from lisa.models import Task
from scripts.benchmark_chat_prompt import messages_for, ordered_check, sha
from scripts.benchmark_dialogue_skill import SkillRun


def fixed_evaluation_clock(instructions: str, timestamp: str) -> str:
    prefix, rest = instructions.split("当前 UTC 时间：", 1)
    _, suffix = rest.split("\n", 1)
    return prefix + "当前 UTC 时间：" + timestamp + "。\n" + suffix


class NativeSkillRun(SkillRun):
    def __init__(self, args, *data):
        super().__init__(args, *data)
        self.settings = self.settings.model_copy(
            update={"model_max_output_tokens": self.settings.dialogue_skill_output_tokens}
        )
        self.native = True
        self.report.update(
            experiment="natural_dialogue_native_transfer_v1",
            validation_scope=(
                "Frozen-candidate model transfer on revealed cases; not fresh confirmation."
            ),
            protocol=self.settings.model_api_protocol,
            generation_output_tokens=self.settings.model_max_output_tokens,
            decisions={},
            wire_checks=[],
        )
        for file in (Path(__file__).resolve(), Path("lisa/chat.py"), Path("lisa/dialogue.py")):
            file = file.resolve()
            key = str(file.relative_to(Path.cwd()))
            self.hashes[key] = sha(file.read_bytes())
            target = self.folder / "inputs" / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.read_bytes())
        self.report["source_sha256"].update(self.hashes)

    def save(self):
        if getattr(self, "native", False):
            self.report.update(
                generation_transport="configured_native_Conversation_schema",
                shared_generation_output_protocol="Conversation JSON; score actual message parts",
            )
            self.report.pop("wire_prompt_hashes", None)
        super().save()

    async def generate(self, group, case, rep, variant, prompt):
        key = f"{group}/{case['id']}/r{rep}/{variant}"
        prior, output = [], {"turns": []}
        try:
            for index, text in enumerate(case["turns"]):
                with Session(self.engine) as db, db.begin():
                    task = Task(
                        user_id=self.uid,
                        kind="model_probe",
                        source="development",
                        status="completed",
                        request={"instructions_only": True},
                        input_snapshot={
                            "persona": {
                                "name": "合成LISA",
                                "prompt": "自然直率，有自己的看法。",
                                "version": 1,
                            },
                            "style_card": {"prompt": "自然直率，有自己的看法。"},
                            "dialogue_guide": {
                                "mode": variant,
                                "text": prompt,
                                "sha256": sha(prompt.encode()),
                            },
                        },
                    )
                    db.add(task)
                    db.flush()
                    instructions = _chat_instructions(db, self.uid, task, None, [text])
                # Both arms receive the same evaluation clock, without altering any facts.
                host_prefix = instructions.split("当前 UTC 时间：", 1)[0]
                instructions = fixed_evaluation_clock(instructions, self.report["started_at_utc"])
                assert instructions.startswith(host_prefix)
                self.report["host_instruction_prefix_sha256"] = sha(host_prefix.encode())
                self.report["host_instruction_prefix_preserved"] = True
                provider = self._frozen_model_calls._provider_request

                async def inspect(*args, **kwargs):
                    response = await provider(*args, **kwargs)
                    normalized = (
                        self._frozen_model_calls._completion_response(response)
                        if kwargs["endpoint"] == "chat.completions"
                        else response
                    )
                    check = {"label": key, "endpoint": kwargs["endpoint"]}
                    try:
                        raw = json.loads(normalized.output_text)
                        Conversation.model_validate(raw, strict=True)
                        check.update(
                            strict_valid=True,
                            all_fields=set(raw) == set(Conversation.model_fields),
                            intervals_match=len(raw["messages"]) == len(raw["message_intervals"]),
                        )
                    except Exception as exc:
                        check.update(strict_valid=False, error=type(exc).__name__)
                    self.report["wire_checks"].append(check)
                    self.save()
                    return response

                self._frozen_model_calls._provider_request = inspect
                try:
                    value = await self.call(
                        key + f"/t{index}",
                        instructions,
                        messages_for(case, prior, text),
                        Conversation,
                    )
                finally:
                    self._frozen_model_calls._provider_request = provider
                delivered = "\n".join(_message_parts(value, value.reply))
                self.report["decisions"][key + f"/t{index}"] = value.model_dump(mode="json")
                output["turns"].append(delivered)
                prior.extend(
                    [{"role": "user", "content": text}, {"role": "assistant", "content": delivered}]
                )
            output["ordered_check"] = ordered_check(case, output["turns"])
        except Exception as exc:
            output["error"] = type(exc).__name__
        self.report["generations"][key] = output
        self.report["last_generation_at_utc"] = datetime.now(UTC).isoformat()
        self.save()
        return output
