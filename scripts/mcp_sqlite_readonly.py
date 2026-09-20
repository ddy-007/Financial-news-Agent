"""只读 SQLite MCP server —— 开发期查库用。

**为什么自己写，而不是用现成的包**（这是查过行业实践后的决定）：

企业级把第三方 MCP server 当作**不可信的第三方可执行代码**。2026 年一项对
1,899 个开源 MCP server 的实证研究：7.2% 含漏洞、5.5% 存在工具投毒、
66% 有代码异味；2025-09 的 Postmark 事件是一个**通过了功能测试**、
却静默外泄数据的 npm 包。而官方那个 `@modelcontextprotocol/server-sqlite`
**已从 npm 下架**（404）。

更关键的是：**「只读」是本工具的硬要求**，而行业共识是"只读必须由服务端机制
保证，不能信任供应商的 --readonly 标志"——工具投毒恰恰能绕过这种信任。
自己实现才能从机制上锁死。

**只读的保证有两层，都在机制里**（不靠对 SQL 文本的匹配）：

  1. `set_authorizer` —— 在 **SQLite 引擎层**按操作类别拒绝：
     INSERT/UPDATE/DELETE、全部 `CREATE_*`/`DROP_*`、ALTER/REINDEX/ANALYZE、
     TRANSACTION/SAVEPOINT、**ATTACH/DETACH**（PRAGMA 另走只读白名单）。
     它**不看 SQL 文本**，所以「CTE 后接写操作」、注释里藏语句、将来新增的
     语法都绕不过它。
  2. `file:...?mode=ro` —— 主库的写入被 SQLite 自身拒绝。
     注意它**只管主库**：`ATTACH DATABASE` 挂进来的别库不受约束，
     所以第 1 层里对 ATTACH 的拦截是必要的，不是冗余。

另有两道**辅助**（不是安全边界）：
  · SQL 文本前缀白名单 —— 只为给更清楚的报错文案。它拦得住 `DELETE ...`，
    但**拦不住** `WITH x AS (...) INSERT ...`（SQLite 合法语法），后者由第 1 层兜住
  · 结果行数上限 —— 防一条查询把内存打满

⚠️ 别把文本白名单当安全边界。这条注释是纠正过来的 —— 原文曾把第 3 条写成
"写操作一律拒绝"，属**过度声明**（实测那条 CTE 语句能通过前缀检查）。
教训：**提示是建议，机制才是规则。**

**启动方式**（见项目根的 `.mcp.json`）：
    uv run --no-project --with "mcp>=2,<3" python scripts/mcp_sqlite_readonly.py

⚠️ **只适用于默认库路径**：数据库固定解析为 `<项目根>/data/app.db`。
若 `.env` 用 `DATABASE_URL` 指向了别处，本工具查的库会与 app 实际用的库不是同一个。

数据库路径按**本文件位置**推算（`<项目根>/data/app.db`），不含任何本机绝对路径，
所以本文件可以安全提交到仓库。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

# mcp SDK 2.x：类名是 MCPServer（1.x 叫 FastMCP，已改名）。
# .mcp.json 里把依赖锁成 mcp>=2,<3 —— 避免将来 3.x 再改名时这里静默失效。
from mcp.server.mcpserver import MCPServer

# 按本文件位置推算项目根，避免写死本机路径
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"

MAX_ROWS = 1000          # 单次查询返回行数上限
DEFAULT_ROWS = 200

mcp = MCPServer("sqlite-readonly")


# SQLite 引擎层要拒绝的操作类别（authorizer 的 action 参数）。
# 这一层不看 SQL 文本，因此**不依赖白名单是否被绕过** —— 是真正的机制保证。
_DENY_ACTIONS = frozenset({
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX, sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_VTABLE,
    # ATTACH/DETACH 尤其要拦：mode=ro 只作用于**主库**，挂进来的别的库不受它约束
    sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
})

# PRAGMA 单独处理：describe_table 需要 table_info，只放行只读的那几个。
_ALLOWED_PRAGMAS = frozenset({
    "table_info", "table_xinfo", "index_list", "index_info",
    "foreign_key_list", "database_list",
})


def _authorizer(action: int, arg1, arg2, dbname, source) -> int:
    """SQLite 引擎层的只读闸门。

    对**每一次**底层操作回调（不是对 SQL 文本做正则），所以
    `WITH ... INSERT`、藏在注释里的语句、将来新增的语法都绕不过它。
    """
    if action == sqlite3.SQLITE_PRAGMA:
        name = str(arg1 or "").lower()
        return sqlite3.SQLITE_OK if name in _ALLOWED_PRAGMAS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY if action in _DENY_ACTIONS else sqlite3.SQLITE_OK


def _connect() -> sqlite3.Connection:
    """以**只读模式**打开数据库。

    两层保护：
      · `mode=ro` —— 主库的写入被 SQLite 自己拒绝
      · `set_authorizer` —— 在引擎层拒绝写/DDL/ATTACH（不依赖 SQL 文本）
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"数据库不存在：{DB_PATH}")
    # safe="/:" 保留路径分隔符与 Windows 盘符冒号，其余（如空格）做转义
    uri = f"file:{quote(DB_PATH.as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_authorizer)
    return conn


_READ_ONLY_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def _reject_reason(sql: str) -> str | None:
    """放行返回 None，否则返回拒绝原因。

    **这一层只是「更清楚的报错文案」，不是安全边界。** 真正的保证在
    `_authorizer`（SQLite 引擎层）与 `sqlite3.execute` 自身。

    曾经这里还查过「语句里有没有分号」，现已删除 —— 它是**纯冗余**：
    `sqlite3.execute()` 本来就拒绝多语句（`You can only execute one
    statement at a time`），而它带来的只有误伤（`LIKE '%;%'` 这种
    字符串字面量里的分号会被错杀）。三层防护里唯一只减分不加分的。
    """
    s = sql.strip().rstrip(";").strip()
    if not s:
        return "SQL 为空"
    if not _READ_ONLY_RE.match(s):
        return "只允许 SELECT / WITH 开头的只读查询"
    return None


def _q(ident: str) -> str:
    """把标识符安全地放进双引号里（SQLite 用 `""` 转义双引号）。

    表名来自 sqlite_master 而非用户输入，但万一库里有名字含 `"` 的表，
    不转义会把语句结构撑破 —— 转义是一行的事，不做没道理。
    """
    return '"' + ident.replace('"', '""') + '"'


@mcp.tool()
def list_tables() -> str:
    """列出数据库里所有表，以及每张表的行数。

    想了解库的结构时先调用这个，再用 describe_table 看某张表的字段。
    """
    conn = _connect()
    try:
        names = [
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out = []
        for n in names:
            cnt = conn.execute(f"SELECT COUNT(*) AS c FROM {_q(n)}").fetchone()["c"]
            out.append({"table": n, "rows": cnt})
        return json.dumps(out, ensure_ascii=False)
    finally:
        conn.close()


@mcp.tool()
def describe_table(table: str) -> str:
    """查看某张表的字段定义（列名、类型、是否可空、主键）。

    table 必须是 list_tables 返回的表名之一。
    """
    conn = _connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return f"表不存在：{table!r}（先用 list_tables 看有哪些表）"
        cols = conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()
        return json.dumps(
            [{"name": c["name"], "type": c["type"],
              "nullable": not c["notnull"], "pk": bool(c["pk"])} for c in cols],
            ensure_ascii=False,
        )
    finally:
        conn.close()


@mcp.tool()
def query(sql: str, limit: int = DEFAULT_ROWS) -> str:
    """执行一条**只读** SQL（SELECT / WITH）。

    返回 JSON 对象：`{"rows": [...], "returned": N, "truncated": bool}`。
    **`truncated` 为 true 时说明结果还有更多行没返回**，请据此判断是否
    需要收窄条件（比如加 WHERE / 聚合），不要拿不完整的结果下结论。

    只允许单条 SELECT 或 WITH 语句；写操作、DDL、PRAGMA、多语句都会被拒绝
    （SQLite 引擎层还有 authorizer 兜底，不依赖这里的前缀检查）。
    limit 默认 200，上限 1000。
    """
    reason = _reject_reason(sql)
    if reason:
        return f"已拒绝：{reason}\n收到的 SQL：{sql.strip()[:200]}"

    try:
        n = max(1, min(int(limit), MAX_ROWS))
    except (TypeError, ValueError):
        return f"limit 必须是整数，收到：{limit!r}"

    conn = None
    try:
        conn = _connect()          # 放进 try：库被删/被占用时给友好提示而非抛栈
        cur = conn.execute(sql.strip().rstrip(";"))
        # 多取一行用来判断是否被截断，再丢掉
        rows = cur.fetchmany(n + 1)
        truncated = len(rows) > n
        return json.dumps(
            {"rows": [dict(r) for r in rows[:n]],
             "returned": min(len(rows), n),
             "truncated": truncated},
            ensure_ascii=False, default=str,
        )
    except sqlite3.Error as e:
        return f"SQL 执行失败：{type(e).__name__}: {e}"
    except OSError as e:
        return f"数据库不可用：{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    mcp.run()
