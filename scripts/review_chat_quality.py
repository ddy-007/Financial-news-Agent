"""Review chat answers locally using X-Request-ID and application logs.

Grounding is supported, partial, unsupported or unknown. Risk is complete,
omitted, not_applicable or unknown. An audit verdict may only be supplied
for an audit of the same answer. Daily-report Critic results do not qualify.
"""
import argparse
import json
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

if __package__:
    from .summarize_chat_latency import read_chat_requests, summarize_requests
else:
    from summarize_chat_latency import read_chat_requests, summarize_requests

GROUNDING = ("supported", "partial", "unsupported", "unknown")
RISK = ("complete", "omitted", "not_applicable", "unknown")
AUDIT = ("not_audited", "passed", "rejected")


def load_reviews(path):
    reviews = {}
    try:
        stream = path.open(encoding="utf-8")
    except FileNotFoundError:
        return reviews
    with stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                request_id = row["request_id"]
                elapsed = row["elapsed_ms"]
                if (not re.fullmatch(r"[a-f0-9]{32}", request_id)
                        or row["outcome"] != "answered" or row["grounding"] not in GROUNDING
                        or row["risk"] not in RISK or row["audit"] not in AUDIT
                        or type(elapsed) not in {int, float} or not isfinite(elapsed) or elapsed < 0):
                    raise ValueError("invalid record")
            except (KeyError, TypeError, ValueError, OverflowError):
                raise ValueError(f"Invalid review record at {path}:{line_number}") from None
            reviews[request_id] = row  # An amended review replaces the previous label in summaries.
    return reviews


def summarize_reviews(reviews):
    grounding = Counter()
    risk = Counter()
    audit = Counter()
    paired = missed = false_rejections = 0
    for row in reviews.values():
        evidence, risks, verdict = row["grounding"], row["risk"], row["audit"]
        grounding[evidence] += 1
        risk[risks] += 1
        audit[verdict] += 1
        if verdict == "not_audited" or evidence == "unknown" or risks == "unknown":
            continue
        paired += 1
        human_passed = evidence == "supported" and risks in {"complete", "not_applicable"}
        missed += int(verdict == "passed" and not human_passed)
        false_rejections += int(verdict == "rejected" and human_passed)
    audited = audit["passed"] + audit["rejected"]
    return {
        "reviewed_answers": len(reviews),
        "grounding": dict(grounding), "risk": dict(risk), "answer_audit": dict(audit),
        "audit_pass_rate": audit["passed"] / audited if audited else None,
        "paired_known_reviews": paired,
        "missed_issues": missed if paired else None,
        "false_rejections": false_rejections if paired else None,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Compare the answer to the actual source text before recording labels. "
               "Use unknown when the source text or risk assessment is inconclusive.",
    )
    parser.add_argument("--log", action="append", type=Path, help="Application log, repeatable (oldest first)")
    parser.add_argument("--reviews", type=Path, default=Path("data/chat-quality/reviews.jsonl"),
                        help="Local JSONL file (default: data/chat-quality/reviews.jsonl)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show the latest 10 answered requests in the supplied logs")
    commands.add_parser("summary", help="Summarize supplied logs and all reviews in the local file separately")
    record = commands.add_parser("record", help="Append or amend a manual review of a successful answer")
    record.add_argument("request_id")
    record.add_argument("--grounding", choices=GROUNDING, required=True)
    record.add_argument("--risk", choices=RISK, required=True)
    record.add_argument("--audit", choices=AUDIT, help="Keep the prior verdict if omitted; new answers start unaudited")
    args = parser.parse_args()

    try:
        requests = read_chat_requests(args.log or [Path("logs/app.log")])
        reviews = load_reviews(args.reviews)
        if args.command == "list":
            result = [
                {"request_id": request_id, "elapsed_ms": item.elapsed_ms, "reviewed": request_id in reviews}
                for request_id, item in requests.items() if item.outcome == "answered"
            ][-10:][::-1]
        elif args.command == "summary":
            result = {"server_requests": summarize_requests(requests), "manual_reviews": summarize_reviews(reviews)}
        else:
            request = requests.get(args.request_id)
            if request is None or request.outcome != "answered":
                parser.error("The request ID must identify an answered request in the supplied logs")
            result = {
                "request_id": args.request_id, **asdict(request),
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "grounding": args.grounding, "risk": args.risk,
                "audit": args.audit or reviews.get(args.request_id, {}).get("audit", "not_audited"),
            }
            args.reviews.parent.mkdir(parents=True, exist_ok=True)
            with args.reviews.open("a+b") as stream:
                separator = b""
                if stream.tell():
                    stream.seek(-1, 2)
                    if stream.read(1) != b"\n":
                        separator = b"\n"
                stream.write(separator + (json.dumps(result) + "\n").encode("utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
