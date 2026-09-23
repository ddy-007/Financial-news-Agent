"""LLM 主备切换的判定契约自检（2026-09-23）。

用法：`.venv/Scripts/python.exe scripts/check_llm_contracts.py`

**完全不联网、不调 LLM** —— 只检查「链是怎么搭的」与冷却状态机，不发请求。

**为什么要钉它**（用户 2026-09-23 从日志发现）：`with_fallbacks` **每次调用都从链首
重来** —— 主模型挂了之后，每一批仍要先**白打一次主模型**才切到备用。实机那轮
第 27–34 批每批各打一次无用请求（8 次），外加 8 条重复 WARNING。

现在的行为是「主模型失败 → 冷却 N 分钟，期内把它从链首摘掉」。
这是**状态机**：开着 / 冷却中 / 到期半开。状态机靠人记着同步最容易出错，
而它错了的表现是**静默的**（要么白打请求，要么永远不用主模型）。
"""
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

import app.agent.llm as L  # noqa: E402
from app.config import settings  # noqa: E402

_RESULTS: list[tuple[bool, str]] = []
PART = "news"


def check(label: str, got, want, contract: str = "") -> None:
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if contract:
        print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


@contextmanager
def fake_fallbacks(names: list[str]):
    """临时把该分组的备用列表换掉 —— 让本脚本不依赖 `.env` 里配了什么。"""
    old = L.effective_fallback_models
    L.effective_fallback_models = lambda part: list(names)
    try:
        yield
    finally:
        L.effective_fallback_models = old


def reset_cooldown():
    L._PRIMARY_COOLDOWN.clear()


def head_and_fallbacks():
    llm = L.get_llm(temperature=0.0, part=PART)
    head = getattr(getattr(llm, "runnable", None), "model_name", None)
    fbs = [getattr(f, "model_name", None) for f in (getattr(llm, "fallbacks", None) or [])]
    return head, fbs


def main() -> int:
    primary = L.resolve(PART)[2]

    with fake_fallbacks(["FB-1", "FB-2"]):
        print("\n[1] 没有冷却时：主模型在链首")
        reset_cooldown()
        head, fbs = head_and_fallbacks()
        check("链首是主模型", head, primary)
        check("备用列表完整", fbs, ["FB-1", "FB-2"])

        print("\n[2] 主模型失败 → 开冷却")
        until = L.note_primary_failed(PART)
        check("第一次失败 → 返回解冻时刻（非 None）", until is not None, True,
              "返回非 None = **这是新开的一次**，调用方据此打日志")
        check("解冻时刻 ≈ 现在 + 配置的分钟数",
              abs((until - datetime.now()).total_seconds()
                  - settings.llm_primary_cooldown_minutes * 60) < 5, True)
        check("再调一次 → None（**不续期**）", L.note_primary_failed(PART), None,
              "冷却窗口从第一次失败起固定；否则持续失败会把窗口越推越远")

        print("\n[3] 冷却期内：把主模型从链首摘掉")
        head, fbs = head_and_fallbacks()
        check("链首变成第一个备用", head, "FB-1",
              "这是本改动的全部目的 —— 不再每批白打一次主模型")
        check("剩余备用留在链上", fbs, ["FB-2"], "降级顺序不能丢")

        print("\n[4] 冷却到期 → 半开，主模型回到链首")
        L._PRIMARY_COOLDOWN[PART] = datetime.now() - timedelta(seconds=1)
        head, fbs = head_and_fallbacks()
        check("链首恢复主模型", head, primary)
        check("备用列表也恢复完整", fbs, ["FB-1", "FB-2"])
        check("过期的冷却记录被清掉", PART in L._PRIMARY_COOLDOWN, False,
              "不清的话会一直堆着；且 `_primary_cooling` 的返回值会被误判")

        print("\n[5] 配 0 = 关闭该行为")
        reset_cooldown()
        old_min = settings.llm_primary_cooldown_minutes
        try:
            settings.llm_primary_cooldown_minutes = 0
            check("配 0 时不冷却（行为与改动前一致）",
                  L.note_primary_failed(PART), None)
            head, _ = head_and_fallbacks()
            check("链首仍是主模型", head, primary)
        finally:
            settings.llm_primary_cooldown_minutes = old_min

        print("\n[6] 冷却期内不再重复打 WARNING")
        reset_cooldown()
        L.note_primary_failed(PART)          # 开冷却（这一步会打一条）

        class Sink:
            def __init__(self):
                self.lines = []

            def write(self, msg):
                self.lines.append(str(msg))

        sink = Sink()
        hid = logger.add(sink.write, level="WARNING", format="{message}")
        try:
            rec = L._ModelRecorder(PART, primary)
            # 冷却期内主模型根本不在链上，每批都会走到 `name != primary` 分支
            for _ in range(8):
                rec.on_chat_model_start({}, [], metadata={"ls_model_name": "FB-1"})
        finally:
            logger.remove(hid)
        check("8 次「非主模型启动」→ **0 条**新 WARNING",
              len([ln for ln in sink.lines if "冷却" in ln or "未成功" in ln]), 0,
              "实测那轮打了 8 条重复 WARNING —— 它们把别的信息淹了")

        print("\n[6b] 冷却期内 `get_llm` 本身也不许刷屏（2026-09-24）")
        # `get_llm` 每批都调一次，冷却期内每批都会走「跳过主模型」那一支。
        # 实测 2026-09-24 那轮：**6 秒 15 条**同样的 WARNING —— 与 B1 ③ 是同一个毛病。
        reset_cooldown()
        L.note_primary_failed(PART)
        sink2 = Sink()
        hid2 = logger.add(sink2.write, level="WARNING", format="{message}")
        try:
            for _ in range(8):
                head_and_fallbacks()
        finally:
            logger.remove(hid2)
        check("冷却期内 8 次 `get_llm` → **0 条** WARNING",
              len([ln for ln in sink2.lines if "冷却" in ln]), 0,
              "「进入冷却」那一条已由 `_ModelRecorder` 在**新开冷却时**打过一次；"
              "每批重复说一遍不增加任何信息，只会把别的信息淹掉")

        print("\n[7] 模型名溯源不被冷却影响")
        _head, _ = head_and_fallbacks()
        with fake_fallbacks(["FB-1"]):
            check("`_USED_MODEL` 记的是**备用**名（冷却时链首就是它）",
                  L._USED_MODEL.get(PART), "FB-1",
                  "`get_llm_model_name` 靠它溯源；冷却时若不记，就会谎报成主模型")

    reset_cooldown()
    failed = [n for ok, n in _RESULTS if not ok]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
