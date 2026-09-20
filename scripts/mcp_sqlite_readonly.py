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

**只读有三重保证**（全部在代码里，不靠调用方自觉）：
  1. SQLite 用 `file:...?mode=ro` 打开 —— 写入尝试被**数据库本身**拒绝
  2. 只放行**单条** `SELECT` / `WITH` 语句；多语句、写操作、DDL、PRAGMA 一律拒绝
  3. 结果行数有上限，避免一条查询把内存打满

**启动方式**（见项目根的 `.mcp.json`）：
    uv run --no-project --with mcp python scripts/mcp_sqlite_readonly.py

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


def _connect() -> sqlite3.Connection:
    """以**只读模式**打开数据库。

    `mode=ro` 是 SQLite 自身的能力：即使有人绕过下面的 SQL 检查，
    任何写入也会被数据库直接拒绝（报 attempt to write a readonly database）。
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"数据库不存在：{DB_PATH}")
    # safe="/:" 保留路径分隔符与 Windows 盘符冒号，其余（如空格）做转义
    uri = f"file:{quote(DB_PATH.as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_READ_ONLY_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def _reject_reason(sql: str) -> str | None:
    """放行返回 None，否则返回拒绝原因。"""
    s = sql.strip().rstrip(";").strip()
    if not s:
        return "SQL 为空"
    if ";" in s:
        # 注：字符串字面量里的分号也会被拒（如 LIKE '%;%'）。
        # 这是刻意的保守取舍 —— 开发期查库不值得为这点便利放开多语句。
        return "只允许单条语句（检测到分号）"
    if not _READ_ONLY_RE.match(s):
        return "只允许 SELECT / WITH 开头的只读查询"
    return None


def _rows_to_json(rows) -> str:
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)


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
            # 表名来自 sqlite_master，不是用户输入；仍用引号包裹以防特殊字符
            cnt = conn.execute(f'SELECT COUNT(*) AS c FROM "{n}"').fetchone()["c"]
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
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return json.dumps(
            [{"name": c["name"], "type": c["type"],
              "nullable": not c["notnull"], "pk": bool(c["pk"])} for c in cols],
            ensure_ascii=False,
        )
    finally:
        conn.close()


@mcp.tool()
def query(sql: str, limit: int = DEFAULT_ROWS) -> str:
    """执行一条**只读** SQL（SELECT / WITH），返回 JSON 数组。

    只允许单条 SELECT 或 WITH 语句；写操作、DDL、PRAGMA、多语句都会被拒绝
    （数据库本身也以只读方式打开，是第二重保险）。
    limit 默认 200，上限 1000。
    """
    reason = _reject_reason(sql)
    if reason:
        return f"已拒绝：{reason}\n收到的 SQL：{sql.strip()[:200]}"

    n = max(1, min(int(limit), MAX_ROWS))
    conn = _connect()
    try:
        cur = conn.execute(sql.strip().rstrip(";"))
        return _rows_to_json(cur.fetchmany(n))
    except sqlite3.Error as e:
        return f"SQL 执行失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
