"""Summarize completed chat handler executions from local application logs.

Latency percentiles cover answered requests only. This does not measure client
wait time, requests rejected before the handler, or unfinished requests.
"""
import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from math import ceil
from pathlib import Path

_ENTRY = re.compile(
    r"\[chat_request\] request_id=([a-f0-9]{32}) elapsed_ms=(\d+\.\d+) "
    r"outcome=(answered|empty|error) error_kind=(none|timeout|execution)\s*$"
)


@dataclass(frozen=True)
class ChatRequestRecord:
    elapsed_ms: float
    outcome: str
    error_kind: str


def read_chat_requests(paths: list[Path]) -> dict[str, ChatRequestRecord]:
    requests = {}
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as log:
            for line in log:
                match = _ENTRY.search(line)
                if not match:
                    continue
                request_id, elapsed_ms, outcome, error_kind = match.groups()
                requests.setdefault(request_id, ChatRequestRecord(float(elapsed_ms), outcome, error_kind))
    return requests


def summarize_requests(requests: dict[str, ChatRequestRecord]) -> dict:
    outcomes = Counter()
    errors = Counter()
    answered_ms = []
    for record in requests.values():
        outcomes[record.outcome] += 1
        if record.outcome == "answered":
            answered_ms.append(record.elapsed_ms)
        if record.error_kind != "none":
            errors[record.error_kind] += 1

    answered_ms.sort()
    count = len(answered_ms)
    return {
        "completed_requests": len(requests),
        "outcomes": dict(outcomes),
        "error_kinds": dict(errors),
        "answered_latency_ms": {
            "min": answered_ms[0],
            "p50": answered_ms[ceil(0.50 * count) - 1],
            "p95": answered_ms[ceil(0.95 * count) - 1],
            "max": answered_ms[-1],
        } if count else None,
    }


def summarize(paths: list[Path]) -> dict:
    return summarize_requests(read_chat_requests(paths))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="*", type=Path, default=[Path("logs/app.log")])
    args = parser.parse_args()
    try:
        summary = summarize(args.logs)
    except OSError as exc:
        parser.error(str(exc))
    print(json.dumps(summary, indent=2))
