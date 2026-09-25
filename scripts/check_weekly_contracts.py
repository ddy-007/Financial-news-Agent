"""Offline contract check for weekly risk preservation."""
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.agent import weekly
from app.models.base import Base
from app.models.report import MarketReport


class FakeLLM:
    def invoke(self, _prompt):
        return SimpleNamespace(content=json.dumps({
            "market_summary": "本周综述",
            "risks": ["模型补充风险"],
        }))


def main() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        source_risks = []
        for day, offset in ((21, 0), (23, 12)):
            risks = [f"来源风险 {i}" for i in range(offset, offset + 12)]
            source_risks.extend(risks)
            db.add(MarketReport(
                date=datetime(2026, 9, day, 18, 30),
                report_day=date(2026, 9, day),
                report_type="daily",
                title="日报",
                content=json.dumps({"risks": risks}),
                sentiment="中性",
                score=0.0,
            ))
        db.commit()

        with patch.object(weekly, "get_llm", return_value=FakeLLM()), \
                patch.object(weekly, "get_llm_model_name", return_value="fake"):
            report = weekly.generate_weekly_report(db, end_date=date(2026, 9, 24))

        assert report is not None
        final = json.loads(report.content)
        assert final["daily_count"] == 2
        assert final["risks"] == ["模型补充风险", *source_risks]
        print(f"WEEKLY_CONTRACT_PASS {len(final['risks'])} risks retained")


if __name__ == "__main__":
    main()
