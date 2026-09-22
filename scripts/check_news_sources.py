"""财联社端点实测（设计 §7 前置任务 T）—— **只读**，每个端点只打一次，不发 LLM。

用法：`.venv/Scripts/python.exe scripts/check_news_sources.py`

**背景**：代码现用 `v1/roll/get_roll_list`；外部资料记录该端点直连会报"签名错误"，
推荐改用 `nodeapi/telegraphList`。两者说法冲突，所以**先实测再定主备**，
而不是凭资料改代码。

**决策规则**（写死在这里，避免事后挪动标准）：
- 两者都正常 → 保留 A（v1）为主源（改动最小），B 记为备源
- 仅 B 正常  → B 为主源，A 记为备源
- 两者都异常 → 停止 P2，只做 P0/P1，并把结论报给用户
"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.http_client import close_client, get_client  # noqa: E402

_SV = "8.7.9"


def _sign(params: dict) -> str:
    """财联社签名：参数按键排序 → `k=v&...` → SHA1 → MD5。私有接口，可能随版本变化。"""
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sha1 = hashlib.sha1(query.encode()).hexdigest()
    return hashlib.md5(sha1.encode()).hexdigest()


def _probe(label: str, url: str, params: dict) -> dict:
    """请求一次并汇报。**绝不抛异常** —— 探测脚本本身失败就没意义了。"""
    signed = dict(params, sign=_sign(params))
    print(f"\n--- {label} ---")
    print(f"  URL   : {url}")
    print(f"  参数   : {sorted(k for k in signed if k != 'sign')} + sign")
    try:
        r = get_client().get(url, params=signed)
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 请求异常: {type(e).__name__}: {str(e)[:120]}")
        return {"ok": False, "reason": f"{type(e).__name__}"}

    print(f"  HTTP  : {r.status_code}")
    if r.status_code != 200:
        print(f"  响应体: {r.text[:200]!r}")
        return {"ok": False, "reason": f"HTTP {r.status_code}"}

    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 非 JSON: {type(e).__name__}: {str(e)[:80]}")
        print(f"  响应体: {r.text[:200]!r}")
        return {"ok": False, "reason": "非 JSON"}

    print(f"  顶层字段: {sorted(data.keys()) if isinstance(data, dict) else type(data).__name__}")
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        print(f"  data 字段: {sorted(data['data'].keys())}")

    # 按各端点的已知结构取列表
    rows = []
    for path in (("data", "roll_data"), ("data", "telegraph_list")):
        cur = data
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, list) and cur:
            rows = cur
            print(f"  列表路径: data.{path[1]}（{len(cur)} 条）")
            break
    if not rows and isinstance(data, dict):
        # 兜底：把 data 下第一个是 list 的字段打出来，便于人工判断
        d = data.get("data")
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, list):
                    print(f"  ⚠️ 未匹配已知路径；data.{k} 是列表（{len(v)} 条），首条键: "
                          f"{sorted(v[0].keys())[:12] if v and isinstance(v[0], dict) else '?'}")

    if not rows:
        print("  ❌ 没取到任何条目")
        return {"ok": False, "reason": "空列表"}
    first = rows[0]
    if isinstance(first, dict):
        print(f"  首条字段: {sorted(first.keys())}")
        print(f"  首条 id/ctime/title = {first.get('id')!r} / {first.get('ctime')!r} / "
              f"{str(first.get('title') or first.get('brief'))[:40]!r}")
    return {"ok": True, "rows": len(rows)}


def main() -> int:
    now = int(time.time())
    print("=" * 60)
    print("财联社两端点实测（每端点一次请求，只读）")
    print("=" * 60)

    a = _probe(
        "A：v1/roll/get_roll_list（现有实现）",
        "https://www.cls.cn/v1/roll/get_roll_list",
        {"app": "CailianpressWeb", "os": "web", "sv": _SV,
         "name": "telegraph", "refresh_type": "1", "rn": "20", "last_time": "0"},
    )
    b = _probe(
        "B：nodeapi/telegraphList（外部推荐的替代）",
        "https://www.cls.cn/nodeapi/telegraphList",
        {"app": "CailianpressWeb", "category": "", "lastTime": str(now),
         "last_time": str(now), "os": "web", "refresh_type": "1",
         "rn": "20", "sv": _SV},
    )

    print("\n" + "=" * 60)
    if a["ok"] and b["ok"]:
        verdict = "两者都正常 → 主源保留 A（v1），B 记为备源"
    elif b["ok"]:
        verdict = "仅 B 正常 → B 为主源，A 记为备源"
    elif a["ok"]:
        verdict = "仅 A 正常 → 维持现状（A 为主源），无需改动"
    else:
        verdict = "两者都异常 → 停止 P2，只做 P0/P1，并把结论报给用户"
    print(f"A(v1)   : {'✅ 正常 ' + str(a.get('rows')) + ' 条' if a['ok'] else '❌ ' + a['reason']}")
    print(f"B(node): {'✅ 正常 ' + str(b.get('rows')) + ' 条' if b['ok'] else '❌ ' + b['reason']}")
    print(f"\n结论：{verdict}")
    print("=" * 60)

    close_client()
    return 0


if __name__ == "__main__":
    sys.exit(main())
