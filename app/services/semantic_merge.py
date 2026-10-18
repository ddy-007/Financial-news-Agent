"""跨轮语义合并准入：目标簇和候选全部成员均须满足直径与时间窗约束。"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
from loguru import logger

from app.collectors.news_collector import NewsItem
from app.models.news_ingest import RawNews
from app.services.news_archive import decode_item

PAIRWISE_POLICY = "pairwise_v1"


def member_text(item) -> str:
    return f"{item.title}\n{item.content or ''}"


class SemanticMergeGuard:
    """实例仅活于当前采集锁内；准入即更新工作集合，随业务事务一同落库。"""

    def __init__(self, db, embed, vectors, *, threshold=0.85, max_size=20, window_hours=24):
        self.db = db
        self.embed = embed
        self.vectors = dict(vectors)
        self.threshold = float(np.float32(threshold))
        self.max_size = max_size
        self.window = timedelta(hours=window_hours)
        self.targets = {}
        self.rejected = 0

    @staticmethod
    def unique(items):
        # 同文同发布时间的多来源副本不增加语义规模；不同发布时间仍独立检查。
        return list({(it.publish_time, member_text(it)): it for it in items}.values())

    def consistent(self, items) -> bool:
        members = self.unique(items)
        if not members or len(members) > self.max_size:
            return False
        if any(it.publish_time is None for it in members):
            return False
        try:
            too_wide = max(it.publish_time for it in members) - min(it.publish_time for it in members) > self.window
        except TypeError:
            return False
        if too_wide:
            return False
        texts = list(dict.fromkeys(member_text(it) for it in members))
        missing = [text for text in texts if text not in self.vectors]
        if missing:
            try:
                encoded = np.asarray(self.embed(missing), dtype=np.float32)
                if encoded.ndim != 2 or len(encoded) != len(missing):
                    raise ValueError("成员嵌入维度或条数错误")
                for text, vec in zip(missing, encoded):
                    norm = np.linalg.norm(vec)
                    self.vectors[text] = (vec / norm if np.isfinite(vec).all()
                                          and np.isfinite(norm) and norm > 0 else None)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[去重] 成员嵌入不可用，拒绝自动合并：{type(exc).__name__}")
                self.vectors.update({text: None for text in missing})
        vectors = [self.vectors[text] for text in texts]
        if any(vec is None for vec in vectors):
            return False
        try:
            matrix = np.stack(vectors).astype(np.float32, copy=False)
        except ValueError:
            return False
        if not np.isfinite(matrix).all() or np.any(np.linalg.norm(matrix, axis=1) == 0):
            return False
        similarities = matrix @ matrix.T
        np.fill_diagonal(similarities, 1.0)
        return bool(np.all(similarities >= self.threshold))

    def _members(self, row):
        if row.id in self.targets:
            return self.targets[row.id]
        members = None
        if row.dedup_policy == PAIRWISE_POLICY:
            # 先限量取原文，超限即停止；不把无边界历史簇全部加载/嵌入。
            raw = self.db.query(RawNews).filter(RawNews.news_id == row.id).order_by(
                RawNews.id).limit(self.max_size + 1).all()
            if raw and len(raw) <= self.max_size:
                try:
                    members = [decode_item(r.payload) for r in raw]
                    members.append(NewsItem(title=row.title, content=row.content or "",
                        source=row.source, url=row.url, publish_time=row.publish_time))
                    members = self.unique(members)
                except (TypeError, ValueError, KeyError):
                    members = None
                if members is not None and not self.consistent(members):
                    members = None
            if members is None:
                row.dedup_policy = None
        self.targets[row.id] = members
        return members

    def admit(self, row, items) -> bool:
        existing = self._members(row)
        combined = self.unique(existing + items) if existing is not None else []
        if not existing or not self.consistent(combined):
            self.rejected += 1
            return False
        self.targets[row.id] = combined
        return True

    def identity_update(self, row, items) -> None:
        """确切 URL 身份更新保留；新正文/时间不一致时撤销语义准入资格。"""
        if row.dedup_policy != PAIRWISE_POLICY:
            return
        if not self.admit(row, items):
            row.dedup_policy = None
            self.targets[row.id] = None
