"""端到端验证：配置 → 模型加载 → 采集 → LLM 连通。

用法：.venv/Scripts/python.exe scripts/verify.py
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path，使 `from app...` 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def check_config():
    from app.config import settings

    print(f"  model={settings.deepseek_model}")
    print(f"  bge-m3={settings.bge_m3_model_path}")
    print(f"  reranker={settings.bge_reranker_path}")
    print(f"  db={settings.database_url}")


def check_torch_cuda():
    import torch

    print(f"  torch={torch.__version__} cuda_available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"  gpu={torch.cuda.get_device_name(0)}")


def check_models():
    from app.rag.embeddings import embed_query
    from app.rag.reranker import rerank

    v = embed_query("测试：今天股市上涨")
    print(f"  bge-m3 嵌入维度: {len(v)}")
    s = rerank("股市上涨", ["今天股市大涨", "今天天气很好"])
    print(f"  reranker 分数: {[round(float(x), 4) for x in s]}")


def check_news():
    from app.collectors.news_collector import collect_all_news

    items = collect_all_news()
    print(f"  采集到 {len(items)} 条新闻")
    for i in items[:3]:
        print(f"    - [{i.source}] {i.title[:50]}")


def check_market():
    from app.collectors.market_collector import collect_all_market_data

    rows = collect_all_market_data()
    print(f"  采集到 {len(rows)} 条行情")
    for r in rows[:3]:
        print(f"    - {r['name']}: {r['close']} ({r['change_pct']}%)")


def check_llm():
    from app.agent.llm import get_llm

    llm = get_llm()
    resp = llm.invoke("用一句话回答：今天天气如何？")
    print(f"  DeepSeek 回复: {resp.content[:100]}")


STEPS = [
    ("配置", check_config),
    ("torch/CUDA", check_torch_cuda),
    ("模型加载(嵌入+重排)", check_models),
    ("新闻采集", check_news),
    ("行情采集", check_market),
    ("LLM 连通", check_llm),
]


if __name__ == "__main__":
    failed = 0
    for name, fn in STEPS:
        print(f"\n===== {name} =====")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {type(e).__name__}: {e}")
    print(f"\n{'=' * 40}\n结果: {len(STEPS) - failed}/{len(STEPS)} 项通过")
    sys.exit(1 if failed else 0)
