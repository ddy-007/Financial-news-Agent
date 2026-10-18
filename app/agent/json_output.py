"""模型 JSON 提取：按解码边界扫描，保留字符串中的括号，拒绝歧义候选。"""
import json
import re


def extract_json(text: str, expected_type=None):
    if not isinstance(text, str):
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    types = expected_type or (dict, list)
    try:
        data = json.loads(text)
        return data if isinstance(data, types) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates = []
    pos = 0
    while pos < len(text):
        if text[pos] not in "{[":
            pos += 1
            continue
        try:
            data, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            # 不从损坏外层的内部抢救一个字段作为整份报告。
            # 找到该顶层容器的结束后再继续；容器未闭合则拒绝本次输出。
            stack = []
            quoted = escaped = False
            for index in range(pos, len(text)):
                char = text[index]
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "{[":
                    stack.append(char)
                elif char in "}]":
                    if not stack or (stack[-1], char) not in (("{", "}"), ("[", "]")):
                        return None
                    stack.pop()
                    if not stack:
                        pos = index + 1
                        break
            else:
                return None
            continue
        if isinstance(data, types) and data not in candidates:
            candidates.append(data)
        pos = end  # 外层成功解码后不把嵌套字段误认为另一个候选
    if len(candidates) > 1:
        raise ValueError("LLM 输出包含多个不同 JSON 候选，拒绝猜选")
    return candidates[0] if candidates else None
