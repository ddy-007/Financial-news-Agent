"""研判报告服务 + 预测回测。"""
from __future__ import annotations

import json
from datetime import date

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.graph import generate_daily_report
from app.config import settings
from app.models.market import MarketData
from app.models.report import MarketReport


def run_daily_pipeline(db: Session) -> MarketReport:
    """每日主流程：生成研判报告 → 追加评估日志。

    注：末尾会向 data/eval_log.jsonl 追加一行评估结果（副作用）。
    评估失败不影响主流程。
    """
    report = generate_daily_report(db)
    try:
        from app.services.evaluation import append_eval_log

        append_eval_log(db)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"评估日志追加失败（不影响主流程）: {e}")
    return report


def generate_weekly(db: Session) -> MarketReport | None:
    """生成周报（供调度器调用）。日报不足 2 份时返回 None。"""
    from app.agent.weekly import generate_weekly_report

    return generate_weekly_report(db)


def get_latest_daily(db: Session) -> MarketReport | None:
    """最近一份**日报**（排除周报）。"""
    return (
        db.query(MarketReport)
        .filter((MarketReport.report_type == "daily")
                | (MarketReport.report_type.is_(None)))
        .order_by(MarketReport.date.desc())
        .first()
    )


def get_latest_weekly(db: Session) -> MarketReport | None:
    return (
        db.query(MarketReport)
        .filter(MarketReport.report_type == "weekly")
        .order_by(MarketReport.date.desc())
        .first()
    )


def list_reports(db: Session, limit: int = 30,
                 report_type: str | None = "daily") -> list[MarketReport]:
    q = db.query(MarketReport)
    if report_type == "daily":
        q = q.filter((MarketReport.report_type == "daily")
                     | (MarketReport.report_type.is_(None)))
    elif report_type:
        q = q.filter(MarketReport.report_type == report_type)
    return q.order_by(MarketReport.date.desc()).limit(limit).all()


def upsert_daily_report(db: Session, report_day: date, **fields) -> MarketReport:
    """按 `(report_type='daily', report_day)` 幂等写入日报：**当天已有则原地覆盖**。

    **为什么需要**（H2，2026-09-25）：日报原先是无条件 `db.add(...)`，
    而同日的判定落在 `date`（生成时刻，含微秒）上 —— 于是**同一天可以落多份**。
    实测库里 `2026-09-15` 落了 3 份、`09-14`/`09-06` 各 2 份。
    多份的后果不是报错，是**静默**：`/reports/today` 只返回最后一份，
    更早的结果被隐藏；回测得自己在内存里按天去重，否则同一天被当成多个独立样本
    **重复加权**。

    语义与周报的「同周覆盖更新」保持一致 —— 保留原 `id`，只覆盖内容，
    这样外部引用（若有）不会失效。
    """
    # ⚠️ `report_type` / `report_day` 是**本函数的身份键**，不许经 `fields` 传进来。
    # 不拦的话 `fields` 里的同名键会被 `setattr` 盖到已有行上 ——
    # 实测：传一次 `report_type=None` 就把那一行的类型改成了 NULL，
    # 而 **SQLite 的唯一索引认为 NULL 互不相等**，于是「一天一份」当场失效。
    for _reserved in ("report_type", "report_day"):
        if _reserved in fields:
            raise ValueError(
                f"`upsert_daily_report` 的 `{_reserved}` 由函数自己管理，"
                f"不能经 fields 传入（会覆盖身份键、让唯一索引失效）"
            )
    existing = (
        db.query(MarketReport)
        .filter(MarketReport.report_type == "daily",
                MarketReport.report_day == report_day)
        .first()
    )
    if existing is not None:
        logger.info(
            f"当日（{report_day}）已有日报 id={existing.id}（生成于 {existing.date}）—— "
            f"**原地覆盖**，不再新增一份（H2：`date` 含微秒，原先的「一天一份」没约束住）"
        )
        for k, v in fields.items():
            setattr(existing, k, v)
        report = existing
    else:
        report = MarketReport(report_type="daily", report_day=report_day, **fields)
        db.add(report)
    try:
        db.commit()
    except IntegrityError:
        # 两个进程可能同时查不到同一业务日。唯一索引负责仲裁，
        # 后提交者回滚后重新读取获胜行，再按本次生成结果覆盖。
        db.rollback()
        report = (
            db.query(MarketReport)
            .filter(MarketReport.report_type == "daily",
                    MarketReport.report_day == report_day)
            .first()
        )
        if report is None:
            raise
        for k, v in fields.items():
            setattr(report, k, v)
        db.commit()
    db.refresh(report)
    return report


def report_to_dict(r: MarketReport) -> dict:
    try:
        content = json.loads(r.content)
    except (json.JSONDecodeError, TypeError):
        content = r.content
    # ⚠️ `content` **必须是 dict** —— 前端拿到后无条件 `c.get(...)`。
    # 有两个形态会漏过去（第三方巡检 2026-09-25 发现，并补了第二支）：
    #   ① 非 JSON 文本（历史手工数据 / 纯 markdown）→ `str`
    #   ② **合法 JSON 但顶层是数组**（如 `[1,2]`）→ `list`
    # 两者都会让前端抛 `AttributeError: 'str'/'list' object has no attribute 'get'`，
    # **整页崩掉**。在这里一次性收口，别让每个消费方各自防。
    if not isinstance(content, dict):
        content = {"raw": content} if content else {}
    try:
        experts = json.loads(r.expert_opinions) if r.expert_opinions else []
    except (json.JSONDecodeError, TypeError):
        experts = []
    return {
        "id": r.id,
        "date": r.date.isoformat(),
        # 业务日（H2，2026-09-25）：`date` 是生成时刻、`report_day` 才是「哪一天」。
        # 前端要按天分组/比较时应使用它。
        "report_day": r.report_day.isoformat() if r.report_day else None,
        "report_type": r.report_type or "daily",
        "title": r.title,
        "content": content,
        "sentiment": r.sentiment,
        "confidence": r.confidence,
        "score": r.score,
        "low_info": r.low_info,
        "data_stale": r.data_stale,
        "risk_veto": r.risk_veto,
        "divergence": r.divergence,
        "expert_opinions": experts,
        "model": r.model,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _score_bucket(score: float) -> str:
    """按综合情绪分分档。

    边界取自 config（与 `aggregate_node` 定调、`compute_backtest` 判方向**同源**），
    档位文案随之生成 —— 改配置时文案自动跟上，不会出现"标签写 0.1、实际判 0.15"。
    """
    n, s = settings.score_neutral_band, settings.score_strong_band
    if score > s:
        return f"强多(>{s})"
    if score > n:
        return f"偏多({n}~{s})"
    if score < -s:
        return f"强空(<-{s})"
    if score < -n:
        return f"偏空(-{s}~-{n})"
    return f"中性(-{n}~{n})"


def compute_backtest(db: Session) -> dict:
    """用上证指数次日涨跌回测历史研判方向准确率，并按情绪分档统计胜率。

    注：只统计**日报**——周报与日报同日、会被重复计数，且周报不对应"次日"。
    """
    reports = (
        db.query(MarketReport)
        .filter((MarketReport.report_type == "daily")
                | (MarketReport.report_type.is_(None)))
        .order_by(MarketReport.date.asc())
        .all()
    )
    # ① 先剔掉没有 score 的（聚合失败留下的），② 再按天去重取最后一份。
    # **顺序不能反**：若先去重、而某天最后一份恰好无 score，这一天会整个从回测消失，
    # 而不是退回到当天那份有 score 的。
    scored = [r for r in reports if r.score is not None]
    # 「最后一份」= `date` 最晚的那份（遍历顺序是 date 升序，dict 覆盖即取最后）。
    # 为什么需要去重：`report.date` 存的是**含微秒**的 datetime，唯一约束作用在它上面，
    # 同一天不同秒即不同值 —— 「一天一份」实际没约束住，开发期手动重跑会留下多份。
    # 不去重的话，同一天会被当成多个独立样本，在准确率里被重复加权。
    by_day: dict = {}
    for rep in scored:
        by_day[rep.date.date()] = rep
    reports = list(by_day.values())
    sh = (
        db.query(MarketData)
        .filter(MarketData.symbol == "sh000001")
        .order_by(MarketData.date.asc())
        .all()
    )
    date_to_pct: dict = {r.date.date(): r.change_pct for r in sh}
    dates = sorted(date_to_pct.keys())

    correct = 0
    total = 0
    buckets: dict[str, dict] = {}
    details = []
    for rep in reports:
        # score 为 None 的已在上面去重前剔除（见注释），这里无需再判
        # 边界与 aggregate_node 定调、_score_bucket 分档同源（config 一处控制）
        _n = settings.score_neutral_band
        pred_dir = 1 if rep.score > _n else (-1 if rep.score < -_n else 0)
        if pred_dir == 0:
            continue  # 中性不纳入方向统计
        rd = rep.date.date()
        next_dates = [d for d in dates if d > rd]
        if not next_dates:
            continue
        next_pct = date_to_pct[next_dates[0]]
        if next_pct is None:
            continue
        actual_dir = 1 if next_pct > 0 else (-1 if next_pct < 0 else 0)
        if actual_dir == 0:
            continue
        ok = pred_dir == actual_dir
        total += 1
        correct += int(ok)
        bucket = _score_bucket(rep.score)
        b = buckets.setdefault(bucket, {"total": 0, "correct": 0})
        b["total"] += 1
        b["correct"] += int(ok)
        details.append({
            "date": str(rd),
            "score": rep.score,
            "pred_dir": "看多" if pred_dir > 0 else "看空",
            "next_change_pct": next_pct,
            "correct": ok,
        })

    accuracy = round(correct / total * 100, 2) if total else 0.0
    by_bucket = [
        {
            "bucket": k,
            "total": v["total"],
            "correct": v["correct"],
            "accuracy": round(v["correct"] / v["total"] * 100, 2) if v["total"] else 0.0,
        }
        for k, v in buckets.items()
    ]
    return {
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "by_bucket": by_bucket,
        "details": details,
    }
