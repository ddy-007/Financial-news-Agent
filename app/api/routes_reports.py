"""研判报告相关接口。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import report_service

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


@router.get("/today")
def today(db: Session = Depends(get_db)):
    """最近一份**日报**（不含周报）。"""
    r = report_service.get_latest_daily(db)
    if not r:
        raise HTTPException(status_code=404, detail="暂无研判报告，请先触发生成")
    return report_service.report_to_dict(r)


@router.get("/weekly")
def weekly(db: Session = Depends(get_db)):
    """最近一份周报。"""
    r = report_service.get_latest_weekly(db)
    if not r:
        raise HTTPException(status_code=404, detail="暂无周报（需本周至少 2 份日报）")
    return report_service.report_to_dict(r)


@router.post("/weekly/generate")
def generate_weekly(db: Session = Depends(get_db)):
    """手动生成本周周报。"""
    r = report_service.generate_weekly(db)
    if r is None:
        raise HTTPException(status_code=400, detail="本周日报不足 2 份，无法生成周报")
    return report_service.report_to_dict(r)


@router.get("")
def list_reports(limit: int = Query(30, ge=1, le=200),
                 report_type: str | None = "daily",
                 db: Session = Depends(get_db)):
    """报告列表。report_type: daily / weekly / all"""
    rt = None if report_type == "all" else report_type
    return [
        report_service.report_to_dict(r)
        for r in report_service.list_reports(db, limit, rt)
    ]


@router.post("/generate")
def generate(db: Session = Depends(get_db)):
    """手动触发一次每日研判（采集不在内，可先调采集接口）。"""
    report = report_service.run_daily_pipeline(db)
    return report_service.report_to_dict(report)


@router.get("/backtest")
def backtest(db: Session = Depends(get_db)):
    return report_service.compute_backtest(db)


@router.get("/evaluation")
def evaluation(diagnostic: bool = False, db: Session = Depends(get_db)):
    """评估层：验证风险官与首席的产出。

    diagnostic=true 时额外跑 S1 敏感性 / S2 复现性（会调用 LLM，较慢）。
    """
    from app.services.evaluation import run_all_checks

    return run_all_checks(db, include_diagnostic=diagnostic)
