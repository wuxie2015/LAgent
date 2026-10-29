from dataclasses import dataclass, fields


@dataclass(frozen=True)
class MemoryPolicy:
    recent_turns: int = 10
    context_max_chars: int = 48000
    facts_max_chars: int = 8000
    tasks_max_chars: int = 6000
    tool_context_max_chars: int = 4000
    extraction_max_chars: int = 24000
    maintenance_calls: int = 2
    maintenance_timeout: int = 30
    backlog_warning_turns: int = 30

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 1:
                raise ValueError(f"记忆配置 {field.name} 必须是正整数")
        if self.context_max_chars < 1000 or self.extraction_max_chars < 2000:
            raise ValueError("记忆上下文至少1000字符，提取预算至少2000字符")
