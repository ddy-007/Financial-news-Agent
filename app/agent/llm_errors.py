"""模型错误的有限诊断：只取错误字段，脱敏后记录，不输出请求或认证头。"""
from __future__ import annotations

import re

from app.config import settings


def _redact(value, limit: int = 600) -> str:
    text = str(value or "")
    for name in type(settings).model_fields:
        if "api_key" in name:
            secret = str(getattr(settings, name, "") or "").strip()
            if secret:
                text = text.replace(str(secret), "[redacted]")
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [redacted]", text)
    text = re.sub(r"\bsk-[\w-]+", "[redacted]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|authorization|secret)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+",
        r"\1[redacted]", text,
    )
    return " ".join(text.split())[:limit]


def error_details(exc: BaseException) -> dict:
    """HTTP 错误只读取 error.message/code/param；不使用含完整响应的 repr。"""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
    body = body if isinstance(body, dict) else {}
    message = body.get("message") or getattr(exc, "message", None)
    if not isinstance(message, str):
        message = ""  # 非 HTTP 异常仅留类型，避免校验错误携带完整模型输出。
    return {
        "type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None),
        "code": _redact(body.get("code"), 80),
        "param": _redact(body.get("param"), 80),
        "message": _redact(message),
    }
