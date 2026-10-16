"""定向补采：默认只读预览；--apply 显式处理预览，会调用模型并写库。

例：python -B scripts/backfill_news.py --source 新浪财经 --start "2026-10-15 13:59:56"
      --end "2026-10-15 16:08:02" --output data/backfill-preview.json
应用：python -B scripts/backfill_news.py --apply data/backfill-preview.json --max-items 100
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["新浪财经", "东方财富", "财联社"])
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--max-seconds", type=float, default=120,
                        help="翻页时间预算；当前页请求及重试可能额外耗时")
    parser.add_argument("--request-interval", type=float, default=0.5)
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--max-items", type=int)
    args = parser.parse_args()
    if args.apply:
        if args.max_items is None or args.max_items < 0:
            parser.error("--apply 必须指定非负 --max-items，控制本次处理量")
        from app.db import SessionLocal, engine, init_news_ingest_schema
        from app.services.news_backfill import apply_preview
        from app.services.news_service import collection_guard

        with collection_guard() as acquired:
            if not acquired:
                parser.error("另一次采集或补采仍在运行，本次未处理")
            init_news_ingest_schema(engine)
            with SessionLocal() as db:
                result = apply_preview(db, json.loads(args.apply.read_text(encoding="utf-8")),
                                       max_items=args.max_items)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["pending"] and result["window_covered"] else 2

    if not all((args.source, args.start, args.end, args.output)):
        parser.error("预览必须指定 --source --start --end --output")
    if not 1 <= args.max_pages <= 500 or not 1 <= args.max_seconds <= 3600:
        parser.error("页数上限为 1~500，时间预算为 1~3600 秒")
    if not 0.2 <= args.request_interval <= 10:
        parser.error("请求间隔必须为 0.2~10 秒")
    start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
    if start >= end or start.tzinfo or end.tzinfo or end > datetime.now():
        parser.error("请使用有效的北京时间区间，end 不得晚于当前时间")
    if args.output.exists():
        parser.error("输出文件已存在，请换一个路径以保留之前的预览")

    import httpx
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session
    from app.collectors.news_collector import ClsCollector, EastmoneyCollector, SinaCollector
    from app.config import settings
    from app.services.news_backfill import build_preview

    if not settings.database_url.startswith("sqlite:///"):
        parser.error("只读预览当前仅支持本项目的 SQLite 数据库")
    path = Path(settings.database_url[len("sqlite:///"):]).resolve()
    # 不调用 init_db；旧库还未迁移也可以预览。query_only 防止意外写入。
    import sqlite3
    def readonly_connection():
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection
    engine = create_engine("sqlite://", creator=readonly_connection)
    collectors = {c.source_name: c for c in (SinaCollector(), EastmoneyCollector(), ClsCollector())}
    try:
        with httpx.Client(timeout=10) as client:
            result = collectors[args.source].fetch(client=client, since=start,
                        max_pages=args.max_pages, request_interval=args.request_interval,
                        max_seconds=args.max_seconds)
        with Session(engine) as db:
            preview = build_preview(db, result, start, end,
                                    raw_table_available=inspect(engine).has_table("raw_news"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(preview, output, ensure_ascii=False, indent=2)
    finally:
        engine.dispose()
    print(json.dumps({k: v for k, v in preview.items() if k != "items"}, ensure_ascii=False, indent=2))
    print(f"预览保存到 {args.output}；未调用模型、未写业务数据库。")
    return 0 if preview["window_covered"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
