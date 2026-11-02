"""格式化工具函数。"""
from __future__ import annotations

import json


def _fmt_score(v) -> str:
    """分数格式化：**拿不到就显示占位符，绝不显示成 0**。

    `f"{None:+.2f}"` 会直接抛 `TypeError`（键存在、值为 None 时
    `data.get("score", 0)` 的默认值**不生效** —— 默认值只在键缺失时用）。

    ⚠️ 兜成 `0` 是**错的方向**：`0` 是「中性」这个**真实结论**，
    `None` 是「拿不到」。把后者显示成 `+0.00` 等于凭空造了一个结论
    —— 与本页 `divergence` 那行（`if div is not None else "—"`）保持一致。
    """
    return f"{v:+.2f}" if isinstance(v, (int, float)) else "—"


def _fmt_themes(v):
    if not v:
        return ""
    try:
        themes = json.loads(v)
        if isinstance(themes, list):
            return " · ".join(str(t) for t in themes[:3])
    except Exception:
        pass
    return str(v)
