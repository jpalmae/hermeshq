from datetime import datetime

from hermeshq.schemas.common import ORMModel


class TerminalSessionRead(ORMModel):
    id: str
    agent_id: str
    node_id: str | None
    mode: str
    cwd: str | None
    command_json: list[str]
    status: str
    started_at: datetime
    ended_at: datetime | None
    exit_code: int | None
    input_transcript: str
    output_transcript: str
    transcript_text: str
    created_at: datetime
    updated_at: datetime
