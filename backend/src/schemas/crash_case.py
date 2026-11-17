from pydantic import BaseModel, Field, model_validator


class CrashCaseInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    log_set_id: int = Field(gt=0)
    # Legacy field: everything before the crash is durable and only quiescent
    # checkpoints write pages back. Kept so old clients/scenarios keep working.
    crash_after: int | None = Field(default=None, ge=0, le=200)
    executed_through: int | None = Field(default=None, ge=0, le=200)
    log_flushed_through: int | None = Field(default=None, ge=0, le=200)
    page_flushes: dict[str, int] | None = Field(default=None)

    @model_validator(mode='after')
    def check_positions(self):
        if self.executed_through is None and self.crash_after is None:
            raise ValueError('需要执行位置（executed_through 或旧字段 crash_after）')
        if self.executed_through is not None and self.crash_after is not None \
                and self.executed_through != self.crash_after:
            raise ValueError('执行位置与旧字段崩溃点不一致，请只提供一个')
        for key in (self.page_flushes or {}):
            if not 1 <= len(key) <= 50:
                raise ValueError('页写回配置的键名不合法')
        return self
