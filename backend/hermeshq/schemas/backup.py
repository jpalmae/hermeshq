from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class InstanceBackupCreateRequest(BaseModel):
    passphrase: str = Field(min_length=8, max_length=256)
    include_activity_logs: bool = False
    include_task_history: bool = False
    include_terminal_sessions: bool = False
    include_messaging_sessions: bool = False


class InstanceBackupSummary(BaseModel):
    schema_version: str
    app_version: str
    created_at: datetime
    source_hostname: str
    source_instance_root: str
    included_sections: list[str]
    counts: dict[str, int]
    options: dict[str, bool]
    warnings: list[str] = Field(default_factory=list)
    encrypted_sections: list[str] = Field(default_factory=list)


class InstanceBackupValidationRead(BaseModel):
    valid: bool
    filename: str
    summary: InstanceBackupSummary | None = None
    decrypted_sections: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class InstanceBackupRestoreRead(BaseModel):
    restored: bool
    mode: Literal["replace", "merge"]
    summary: InstanceBackupSummary
    restored_counts: dict[str, int]
    warnings: list[str] = Field(default_factory=list)

