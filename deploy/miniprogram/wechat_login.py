"""Exchange a WeChat login code without logging provider credentials."""

import json
import re
from http.client import HTTPException, HTTPSConnection
from urllib.parse import urlencode


class WeChatUnavailable(Exception):
    pass


class WeChatLoginBlocked(Exception):
    pass


class WeChatRateLimited(Exception):
    pass


def exchange_code(app_id, secret, code):
    query = urlencode({"appid": app_id, "secret": secret, "js_code": code,
                       "grant_type": "authorization_code"})
    conn = HTTPSConnection("api.weixin.qq.com", timeout=5)
    try:
        conn.request("GET", "/sns/jscode2session?" + query)
        response = conn.getresponse()
        if response.status != 200:
            raise WeChatUnavailable
        raw = response.read(8193)
        if len(raw) > 8192:
            raise WeChatUnavailable
        result = json.loads(raw)
    except (OSError, HTTPException, ValueError, RecursionError):
        raise WeChatUnavailable from None
    finally:
        conn.close()

    if not isinstance(result, dict):
        raise WeChatUnavailable
    error = result.get("errcode", 0)
    if error in (40029, 40163):
        return None
    if error == 40226:
        raise WeChatLoginBlocked
    if error == 45011:
        raise WeChatRateLimited
    openid = result.get("openid")
    if error != 0 or not isinstance(openid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", openid):
        raise WeChatUnavailable
    return openid
