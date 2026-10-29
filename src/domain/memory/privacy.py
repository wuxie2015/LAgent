"""Best-effort redaction; never a claim of complete secret detection."""
import re
from typing import Any

KEY = re.compile(r"(?i)(api[_-]?key|token|password|passwd|secret|authorization|credential|密码|密钥|令牌)")
PATTERNS = [
    re.compile(r"(?i)((?:api[_-]?key|token|password|passwd|secret|authorization|密码|密钥|令牌)[\s\"']*[:=：]\s*)([^\s,;\n}\"']+)"),
    re.compile(r"\b(?:sk-[\w-]{8,}|tvly-[\w-]{8,}|gh[pousr]_[\w]{12,})\b"),
    re.compile(r"(?i)Bearer\s+[\w.\-/+=]+"),
    re.compile(r"(?i)(https?://)[^\s/:]+:[^\s/@]+@"),
]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for index, pattern in enumerate(PATTERNS):
            value = pattern.sub((lambda m: m.group(1) + "[REDACTED]") if index == 0 else "[REDACTED]", value)
    return value


def sensitive(value: Any) -> bool:
    return redact(value) != value or "[REDACTED]" in str(value)
