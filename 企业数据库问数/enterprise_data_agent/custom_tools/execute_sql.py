"""Execute SQL tool — runs read-only SELECT queries against SQLite or PostgreSQL."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import os
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_ROWS = 10
MAX_OUTPUT_LENGTH = 50000
DEFAULT_TIMEOUT = 30

_DANGEROUS_KEYWORDS = (
    "DROP", "TRUNCATE", "DELETE", "ALTER", "CREATE", "INSERT", "UPDATE",
    "REPLACE", "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX",
    "GRANT", "REVOKE", "COPY", "CALL", "DO",
)


def _strip_sql_comments(sql: str) -> str:
    no_line = re.sub(r"--[^\n]*", "", sql)
    no_block = re.sub(r"/\*.*?\*/", "", no_line, flags=re.DOTALL)
    return no_block


def _validation_view(sql: str) -> str:
    """Return SQL text suitable for conservative keyword checks."""
    without_comments = _strip_sql_comments(sql)
    without_single_quoted = re.sub(r"'(?:''|[^'])*'", "''", without_comments)
    without_double_quoted = re.sub(r'"(?:""|[^"])*"', '""', without_single_quoted)
    return without_double_quoted.strip().upper()


def _validate_read_only_query(sql: str) -> dict[str, Any] | None:
    sql_for_validation = _validation_view(sql)
    sql_without_comments = _strip_sql_comments(sql).strip()

    if not sql_for_validation:
        return {"status": "error", "error": "SQL query cannot be empty"}

    if ";" in sql_without_comments.rstrip(";"):
        return {
            "status": "error",
            "error": "Only one SQL statement is allowed.",
            "sql": sql,
        }

    if not re.match(r"^(SELECT|WITH)\b", sql_for_validation):
        return {
            "status": "error",
            "error": "Only SELECT or WITH ... SELECT queries are allowed.",
            "sql": sql,
        }

    for keyword in _DANGEROUS_KEYWORDS:
        if re.search(rf"\b{keyword}\b", sql_for_validation):
            return {
                "status": "error",
                "error": f"Only read-only SELECT queries are allowed. Found: {keyword}",
                "sql": sql,
            }
    return None


def _resolve_sqlite_path(db_conn_str: str) -> str:
    parsed = urllib.parse.urlparse(db_conn_str)
    if parsed.scheme != "sqlite":
        return db_conn_str
    if parsed.netloc and parsed.path:
        return f"/{parsed.netloc}{parsed.path}"
    path = urllib.parse.unquote(parsed.path)
    if path.startswith("//"):
        path = path[1:]
    return path


def _detect_engine(db_conn_str: str | None) -> tuple[str | None, str | None, str | None]:
    if db_conn_str is None or not db_conn_str.strip() or db_conn_str.strip().startswith("${env."):
        return None, None, "db_conn_str is required. Configure it as a Resource Bundle value."

    conn_str = db_conn_str.strip()
    lowered = conn_str.lower()
    if lowered.startswith(("postgres://", "postgresql://")):
        return "postgres", conn_str, None
    if lowered.startswith("sqlite://"):
        sqlite_path = _resolve_sqlite_path(conn_str)
        if not sqlite_path:
            return None, None, "sqlite connection string must include a database path."
        return "sqlite", sqlite_path, None

    return None, None, "Unsupported db_conn_str. Use postgresql://... or sqlite:///absolute/path.sqlite."


def _coerce_value(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _format_query_result(
    *,
    sql: str,
    columns: list[str],
    rows: list[Any],
    row_to_dict: Any,
    max_rows: int,
    duration_ms: int,
    engine: str,
) -> dict[str, Any]:
    truncated = len(rows) > max_rows
    visible_rows = rows[:max_rows]
    data = [
        {k: _coerce_value(v) for k, v in row_to_dict(row).items()}
        for row in visible_rows
    ]

    result: dict[str, Any] = {
        "status": "success",
        "engine": engine,
        "command_status": f"SELECT {len(data)}" + ("+" if truncated else ""),
        "sql": sql,
        "columns": columns,
        "data": data,
        "row_count": len(data),
        "total_rows": len(data),
        "truncated": truncated,
        "duration_ms": duration_ms,
    }

    warnings = []
    if not data:
        warnings.append("Query returned 0 rows.")
    if truncated:
        warnings.append("Results truncated due to row limit.")
        result["total_rows"] = f">={len(rows)}"
    if warnings:
        result["warnings"] = warnings
    return result


def _trim_large_result(result: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(result, ensure_ascii=False)) <= MAX_OUTPUT_LENGTH:
        return result

    data = result.get("data", [])
    while len(data) > 1 and len(json.dumps(result, ensure_ascii=False)) > MAX_OUTPUT_LENGTH:
        data = data[:-1]
        result["data"] = data
        result["row_count"] = len(data)
        result["truncated"] = True
    warnings = result.get("warnings", [])
    if not any("length limit" in w for w in warnings):
        warnings.append("Results truncated due to length limit.")
        result["warnings"] = warnings
    return result


def _execute_sqlite(sql: str, db_path: str, timeout: int, max_rows: int) -> dict[str, Any]:
    db_file = Path(db_path)
    if not db_file.exists():
        cwd = Path.cwd()
        candidates: list[str] = []
        search_roots = [cwd, cwd.parent, Path("/app"), Path("/workspace"), Path("/artifacts"), Path("/tmp")]
        seen: set[str] = set()
        for root in search_roots:
            if not root.exists() or str(root) in seen:
                continue
            seen.add(str(root))
            try:
                for found in root.rglob("*.sqlite*"):
                    candidates.append(str(found))
                    if len(candidates) >= 20:
                        break
            except (PermissionError, OSError):
                continue
            if len(candidates) >= 20:
                break

        try:
            cwd_listing = sorted(os.listdir(cwd))[:50]
        except OSError as e:
            cwd_listing = [f"<listdir error: {e}>"]

        return {
            "status": "error",
            "engine": "sqlite",
            "error": f"Database file not found: {db_path}",
            "sql": sql,
            "diagnostics": {
                "db_path_arg": db_path,
                "resolved_abs_path": str(db_file.resolve()),
                "cwd": str(cwd),
                "cwd_entries": cwd_listing,
                "sqlite_candidates": candidates or ["<none found>"],
                "search_roots": [str(r) for r in search_roots if r.exists()],
            },
        }

    start = time.time()
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return {
            "status": "error",
            "engine": "sqlite",
            "error": f"Failed to open database: {e}",
            "sql": sql,
        }

    conn.row_factory = sqlite3.Row
    deadline = time.time() + timeout
    conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 1000)

    try:
        cursor = conn.execute(sql)
        cols = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(max_rows + 1)
        duration_ms = int((time.time() - start) * 1000)
        result = _format_query_result(
            sql=sql,
            columns=cols,
            rows=rows,
            row_to_dict=lambda row: dict(row),
            max_rows=max_rows,
            duration_ms=duration_ms,
            engine="sqlite",
        )
    except sqlite3.OperationalError as e:
        duration_ms = int((time.time() - start) * 1000)
        msg = str(e)
        if "interrupted" in msg.lower():
            result = {
                "status": "timeout",
                "engine": "sqlite",
                "error": f"Query timed out after {timeout}s",
                "sql": sql,
                "duration_ms": duration_ms,
            }
        else:
            result = {
                "status": "error",
                "engine": "sqlite",
                "error": msg,
                "error_type": "OperationalError",
                "sql": sql,
                "duration_ms": duration_ms,
            }
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        result = {
            "status": "error",
            "engine": "sqlite",
            "error": str(e),
            "error_type": type(e).__name__,
            "sql": sql,
            "duration_ms": duration_ms,
        }
    finally:
        conn.close()

    return _trim_large_result(result)


def _execute_postgres(sql: str, database_url: str, timeout: int, max_rows: int) -> dict[str, Any]:
    try:
        import psycopg2
        from psycopg2 import OperationalError, errors
        from psycopg2.extras import RealDictCursor
    except ImportError as e:
        return {
            "status": "error",
            "engine": "postgres",
            "error": "PostgreSQL driver is not installed. Install psycopg2-binary in the runtime image.",
            "error_type": type(e).__name__,
            "sql": sql,
        }

    start = time.time()
    conn = None
    try:
        conn = psycopg2.connect(database_url, connect_timeout=min(timeout, 10))
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET statement_timeout = %s", (timeout * 1000,))
            cursor.execute(sql)
            cols = [d.name for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(max_rows + 1)

        duration_ms = int((time.time() - start) * 1000)
        result = _format_query_result(
            sql=sql,
            columns=cols,
            rows=rows,
            row_to_dict=dict,
            max_rows=max_rows,
            duration_ms=duration_ms,
            engine="postgres",
        )
    except errors.QueryCanceled as e:
        duration_ms = int((time.time() - start) * 1000)
        result = {
            "status": "timeout",
            "engine": "postgres",
            "error": f"Query timed out after {timeout}s",
            "error_type": type(e).__name__,
            "sql": sql,
            "duration_ms": duration_ms,
        }
    except OperationalError as e:
        duration_ms = int((time.time() - start) * 1000)
        result = {
            "status": "error",
            "engine": "postgres",
            "error": str(e),
            "error_type": type(e).__name__,
            "sql": sql,
            "duration_ms": duration_ms,
        }
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        result = {
            "status": "error",
            "engine": "postgres",
            "error": str(e),
            "error_type": type(e).__name__,
            "sql": sql,
            "duration_ms": duration_ms,
        }
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    return _trim_large_result(result)


def execute_sql(
    sql: str,
    db_conn_str: str | None = None,
    timeout: int | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Execute a read-only SQL query against the data source in db_conn_str.

    Args:
        sql: SQL query (SELECT / WITH ... SELECT only).
        db_conn_str: Connection string. Supported schemes:
            postgresql://..., postgres://..., sqlite:///absolute/path.sqlite.
        timeout: Query timeout in seconds (default 30).
        max_rows: Max rows to return (default 10).
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if max_rows is None:
        max_rows = MAX_ROWS

    if not sql or not sql.strip():
        return {"status": "error", "error": "SQL query cannot be empty"}

    validation_error = _validate_read_only_query(sql)
    if validation_error:
        return validation_error

    engine, conn_target, conn_error = _detect_engine(db_conn_str)
    if conn_error:
        return {
            "status": "error",
            "error": conn_error,
            "sql": sql,
        }

    if engine == "postgres":
        result = _execute_postgres(sql, conn_target, timeout, max_rows)
    elif engine == "sqlite":
        result = _execute_sqlite(sql, conn_target, timeout, max_rows)
    else:
        result = {
            "status": "error",
            "error": "Unsupported database engine.",
            "sql": sql,
        }

    logger.info("SQL executed: status=%s engine=%s", result.get("status"), result.get("engine"))
    return result
