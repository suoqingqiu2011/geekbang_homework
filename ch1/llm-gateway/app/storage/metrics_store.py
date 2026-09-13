"""
MetricsStore — 异步 SQLite 指标/追踪/计费/熔断存储（G2+G3 修复）。

对应 SPEC §4.11：
  - WAL 模式 + busy_timeout：并发读写安全
  - INSERT OR IGNORE + 唯一约束：计费"恰好一次"（billing_id）
  - 熔断状态持久化：跨 worker 单一事实来源
  - 单连接复用 + 事件循环线程绑定（aiosqlite 内部处理）
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger("llm-gateway.metrics-store")

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "metrics.db"
)

_TABLES = """
CREATE TABLE IF NOT EXISTS usage_metrics (
    billing_id     TEXT PRIMARY KEY,          -- 幂等计费键（恰好一次）
    trace_id       TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    user_id        TEXT DEFAULT 'anonymous',
    project_id     TEXT DEFAULT 'default',
    endpoint       TEXT DEFAULT 'chat/completions',
    prompt_tokens  INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens   INTEGER NOT NULL,
    complete       INTEGER NOT NULL DEFAULT 1,   -- 是否完整(上游真实)用量
    estimated      INTEGER NOT NULL DEFAULT 0,   -- 是否本地估算(如字符数)而非真实 token
    latency_ms     REAL NOT NULL,
    ttft_ms        REAL NOT NULL,
    cost_usd       REAL NOT NULL,
    attempts       INTEGER DEFAULT 1,
    fallback_used  INTEGER DEFAULT 0,
    stream         INTEGER DEFAULT 0,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS traces (
    trace_id    TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    endpoint    TEXT,
    user_id     TEXT,
    project_id  TEXT,
    client_ip   TEXT,
    model       TEXT,
    provider    TEXT,
    status      TEXT,                          -- success | error | cancelled
    error_code  TEXT,
    latency_ms  REAL,
    payload     TEXT                            -- JSON 快照（元数据，不含原文）
);

CREATE TABLE IF NOT EXISTS error_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT,
    provider    TEXT,
    error_code  TEXT,
    message     TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS circuit_states (
    provider          TEXT PRIMARY KEY,
    state             TEXT NOT NULL,           -- closed | open | half_open
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    opened_at         REAL NOT NULL DEFAULT 0,
    half_open_calls   INTEGER NOT NULL DEFAULT 0,
    updated_at        REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    template_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    content       TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    updated_at    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (template_id, version)
);

CREATE TABLE IF NOT EXISTS daily_aggregates (
    day          TEXT NOT NULL,                -- YYYY-MM-DD
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    requests     INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0.0,
    total_latency_ms REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (day, provider, model)
);

CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_metrics (created_at);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage_metrics (provider);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces (created_at);
CREATE INDEX IF NOT EXISTS idx_error_created ON error_log (created_at);
"""


class MetricsStore:
    """异步 SQLite 存储（WAL）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None
        self._closed = False

    # ── 生命周期 ─────────────────────────────────────────────
    async def initialize(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path, timeout=10.0)
        self._db.row_factory = sqlite3.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA busy_timeout=5000;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        await self._db.executescript(_TABLES)
        await self._migrate_prompt_templates_if_needed()
        await self._migrate_usage_columns_if_needed()
        await self._db.commit()
        logger.info("MetricsStore initialized: %s", self.db_path)

    async def close(self) -> None:
        if self._db is not None and not self._closed:
            await self._db.close()
            self._closed = True

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("MetricsStore.initialize() must be called first")
        return self._db

    # ── 用量与计费（恰好一次）─────────────────────────────────
    async def record_usage(
        self,
        *,
        billing_id: str,
        trace_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        ttft_ms: float = 0.0,
        cost_usd: float = 0.0,
        attempts: int = 1,
        fallback_used: bool = False,
        stream: bool = False,
        complete: bool = True,
        estimated: bool = False,
        user_id: str = "anonymous",
        project_id: str = "default",
    ) -> bool:
        """
        幂等写入用量记录（INSERT OR IGNORE）。

        返回 True=新插入，False=已存在（重复计费被忽略）。
        """
        db = self._require_db()
        total = prompt_tokens + completion_tokens
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO usage_metrics (
                billing_id, trace_id, provider, model, user_id, project_id, endpoint,
                prompt_tokens, completion_tokens, total_tokens, complete, estimated,
                latency_ms, ttft_ms, cost_usd, attempts, fallback_used, stream, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'chat/completions', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                billing_id, trace_id, provider, model, user_id, project_id,
                prompt_tokens, completion_tokens, total, int(complete), int(estimated),
                latency_ms, ttft_ms, cost_usd, attempts, int(fallback_used), int(stream),
                time.time(),
            ),
        )
        await db.commit()
        inserted = cursor.rowcount == 1
        if inserted:
            await self._bump_daily_aggregate(provider, model, total, latency_ms, cost_usd)
        return inserted

    async def _bump_daily_aggregate(
        self, provider: str, model: str, total_tokens: int, latency_ms: float, cost_usd: float
    ) -> None:
        db = self._require_db()
        day = time.strftime("%Y-%m-%d")
        await db.execute(
            """
            INSERT INTO daily_aggregates (day, provider, model, requests, errors, total_tokens, cost_usd, total_latency_ms)
            VALUES (?, ?, ?, 1, 0, ?, ?, ?)
            ON CONFLICT(day, provider, model) DO UPDATE SET
                requests = requests + 1,
                total_tokens = total_tokens + excluded.total_tokens,
                cost_usd = cost_usd + excluded.cost_usd,
                total_latency_ms = total_latency_ms + excluded.total_latency_ms
            """,
            (day, provider, model, total_tokens, cost_usd, latency_ms),
        )
        await db.commit()

    # ── 预算：按实际 token 用量核算累计花费 ────────────────────
    async def query_spent_usd(self, project_id: str) -> float:
        """查询某项目累计真实花费（用于预算路由的 spent_usd）。

        估算行（estimated=1）落库时 cost_usd 已强制为 0，天然不计入累计；
        因此累计值即"实际 token 用量对应的真实花费"。
        """
        db = self._require_db()
        cursor = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS spent FROM usage_metrics WHERE project_id = ?",
            (project_id,),
        )
        row = await cursor.fetchone()
        return float(row["spent"])

    # ── Trace ─────────────────────────────────────────────────
    async def record_trace(self, trace: dict[str, Any]) -> None:
        db = self._require_db()
        await db.execute(
            """
            INSERT OR IGNORE INTO traces (
                trace_id, created_at, endpoint, user_id, project_id, client_ip,
                model, provider, status, error_code, latency_ms, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace.get("trace_id", ""),
                trace.get("timestamp_us", time.time()),
                trace.get("endpoint", "chat/completions"),
                trace.get("user_id", "anonymous"),
                trace.get("project_id", "default"),
                trace.get("client_ip", "unknown"),
                trace.get("model", ""),
                trace.get("provider", ""),
                trace.get("status", "success"),
                trace.get("error_code"),
                trace.get("latency_ms"),
                __import__("json").dumps(trace.get("runtime", {}), ensure_ascii=False),
            ),
        )
        await db.commit()

    async def record_error(
        self, trace_id: str, provider: str, error_code: str, message: str
    ) -> None:
        db = self._require_db()
        await db.execute(
            "INSERT INTO error_log (trace_id, provider, error_code, message, created_at) VALUES (?,?,?,?,?)",
            (trace_id, provider, error_code, message[:1000], time.time()),
        )
        await db.commit()

    # ── 熔断器状态（跨 worker 单一事实来源）────────────────────
    async def get_circuit_state(self, provider: str) -> dict[str, Any]:
        db = self._require_db()
        cursor = await db.execute(
            "SELECT * FROM circuit_states WHERE provider = ?", (provider,)
        )
        row = await cursor.fetchone()
        if row is None:
            return {
                "provider": provider, "state": "closed", "consecutive_failures": 0,
                "opened_at": 0.0, "half_open_calls": 0,
            }
        return dict(row)

    async def should_block_provider(
        self, provider: str, threshold: int, recovery_timeout: float, half_open_max_calls: int
    ) -> tuple[bool, str]:
        """
        熔断判定（对应 SPEC §4.7）。
        返回 (blocked, reason)：reason ∈ {"", "circuit_open", "half_open_probe"}
        """
        db = self._require_db()
        st = await self.get_circuit_state(provider)
        now = time.time()

        if st["state"] == "closed":
            if st["consecutive_failures"] >= threshold:
                await db.execute(
                    """
                    INSERT INTO circuit_states (provider, state, consecutive_failures, opened_at, half_open_calls, updated_at)
                    VALUES (?, 'open', ?, ?, 0, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        state='open', opened_at=excluded.opened_at, updated_at=excluded.updated_at
                    """,
                    (provider, st["consecutive_failures"], now, now),
                )
                await db.commit()
                logger.warning("Circuit OPEN for provider=%s (failures=%d)", provider, st["consecutive_failures"])
                return True, "circuit_open"
            return False, ""

        if st["state"] == "open":
            if now - st["opened_at"] >= recovery_timeout:
                # OPEN → HALF_OPEN，放行一枚探针
                await db.execute(
                    """
                    INSERT INTO circuit_states (provider, state, consecutive_failures, opened_at, half_open_calls, updated_at)
                    VALUES (?, 'half_open', ?, ?, 1, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        state='half_open', half_open_calls=1, updated_at=excluded.updated_at
                    """,
                    (provider, st["consecutive_failures"], now, now),
                )
                await db.commit()
                return False, "half_open_probe"
            return True, "circuit_open"

        # half_open
        if st["half_open_calls"] < half_open_max_calls:
            await db.execute(
                """
                INSERT INTO circuit_states (provider, state, consecutive_failures, opened_at, half_open_calls, updated_at)
                VALUES (?, 'half_open', ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    half_open_calls = half_open_calls + 1, updated_at=excluded.updated_at
                """,
                (provider, st["consecutive_failures"], st["opened_at"], st["half_open_calls"] + 1, now),
            )
            await db.commit()
            return False, "half_open_probe"
        return True, "circuit_open"

    async def record_provider_success(self, provider: str) -> None:
        db = self._require_db()
        st = await self.get_circuit_state(provider)
        if st["state"] in ("half_open", "open"):
            await db.execute(
                """
                INSERT INTO circuit_states (provider, state, consecutive_failures, opened_at, half_open_calls, updated_at)
                VALUES (?, 'closed', 0, 0, 0, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    state='closed', consecutive_failures=0, half_open_calls=0, updated_at=excluded.updated_at
                """,
                (provider, time.time()),
            )
        else:
            await db.execute(
                """
                INSERT INTO circuit_states (provider, state, consecutive_failures, opened_at, half_open_calls, updated_at)
                VALUES (?, 'closed', 0, 0, 0, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures=0, updated_at=excluded.updated_at
                """,
                (provider, time.time()),
            )
        await db.commit()

    async def record_provider_failure(self, provider: str, threshold: int) -> None:
        db = self._require_db()
        st = await self.get_circuit_state(provider)
        failures = st["consecutive_failures"] + 1
        now = time.time()
        if st["state"] == "half_open":
            # 探针失败 → 立即重新 OPEN
            await db.execute(
                """
                INSERT INTO circuit_states (provider, state, consecutive_failures, opened_at, half_open_calls, updated_at)
                VALUES (?, 'open', ?, ?, 0, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    state='open', consecutive_failures=excluded.consecutive_failures,
                    opened_at=excluded.opened_at, half_open_calls=0, updated_at=excluded.updated_at
                """,
                (provider, failures, now, now),
            )
            logger.warning("Circuit RE-OPENED for provider=%s (probe failed)", provider)
        else:
            if failures >= threshold:
                await db.execute(
                    """
                    INSERT INTO circuit_states (provider, state, consecutive_failures, opened_at, half_open_calls, updated_at)
                    VALUES (?, 'open', ?, ?, 0, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        state='open', consecutive_failures=excluded.consecutive_failures,
                        opened_at=excluded.opened_at, half_open_calls=0, updated_at=excluded.updated_at
                    """,
                    (provider, failures, now, now),
                )
                logger.warning("Circuit OPEN for provider=%s (failures=%d)", provider, failures)
            else:
                await db.execute(
                    """
                    INSERT INTO circuit_states (provider, state, consecutive_failures, opened_at, half_open_calls, updated_at)
                    VALUES (?, 'closed', ?, 0, 0, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        consecutive_failures=excluded.consecutive_failures, updated_at=excluded.updated_at
                    """,
                    (provider, failures, now),
                )
        await db.commit()

    # ── CLI 查询（SPEC §13 / M5）──────────────────────────────
    async def query_daily_aggregates(
        self, days: int = 7, provider: str | None = None
    ) -> list[dict[str, Any]]:
        db = self._require_db()
        sql = "SELECT * FROM daily_aggregates WHERE day >= date('now', ?) "
        params: list[Any] = [f"-{days} days"]
        if provider:
            sql += "AND provider = ? "
            params.append(provider)
        sql += "ORDER BY day DESC, cost_usd DESC"
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def query_recent_traces(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        db = self._require_db()
        sql = "SELECT * FROM traces "
        params: list[Any] = []
        if status:
            sql += "WHERE status = ? "
            params.append(status)
        sql += "ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def query_summary(self) -> dict[str, Any]:
        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS requests,
                   SUM(total_tokens) AS total_tokens,
                   SUM(cost_usd) AS total_cost,
                   AVG(latency_ms) AS avg_latency_ms,
                   COUNT(DISTINCT provider) AS providers
            FROM usage_metrics
            """
        )
        row = await cursor.fetchone()
        summary = dict(row) if row else {}
        cursor2 = await db.execute("SELECT COUNT(*) AS c FROM error_log")
        row2 = await cursor2.fetchone()
        summary["errors"] = dict(row2)["c"] if row2 else 0
        return summary

    # ── Prompt 模板（SPEC §4.5，按 template_id/version 保留历史）───────
    async def _migrate_usage_columns_if_needed(self) -> None:
        """旧库 usage_metrics 表补齐 complete/estimated 列（原有行按真实用量处理）。"""
        db = self._require_db()
        info = await (await db.execute("PRAGMA table_info(usage_metrics)")).fetchall()
        have = {row["name"] for row in info}
        if "complete" not in have:
            await db.execute(
                "ALTER TABLE usage_metrics ADD COLUMN complete INTEGER NOT NULL DEFAULT 1"
            )
        if "estimated" not in have:
            await db.execute(
                "ALTER TABLE usage_metrics ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0"
            )

    async def _migrate_prompt_templates_if_needed(self) -> None:
        """旧库中 prompt_templates 以 template_id 单主键存储（内容被覆盖）。

        检测到非 (template_id, version) 联合主键时，重建表并将每个模板
        现有最新内容迁移为 version=1，从而支持保留历史。
        """
        db = self._require_db()
        info = await (await db.execute("PRAGMA table_info(prompt_templates)")).fetchall()
        pk_cols = [row["name"] for row in info if row["pk"] > 0]
        if pk_cols == ["template_id", "version"]:
            return  # 已是联合主键的新结构，无需迁移
        old = await (await db.execute("SELECT * FROM prompt_templates")).fetchall()
        await db.execute("DROP TABLE prompt_templates")
        await db.executescript(_TABLES)
        for row in old:
            await db.execute(
                "INSERT OR IGNORE INTO prompt_templates (template_id, name, content, version, updated_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (row["template_id"], row["name"], row["content"], row["updated_at"]),
            )

    async def upsert_template(self, template_id: str, name: str, content: str) -> int:
        """新增一个模板版本（自增 version），保留历史。返回新版本号。"""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM prompt_templates WHERE template_id = ?",
            (template_id,),
        )
        next_version = int((await cursor.fetchone())["next"])
        await db.execute(
            "INSERT INTO prompt_templates (template_id, name, content, version, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (template_id, name, content, next_version, time.time()),
        )
        await db.commit()
        return next_version

    async def get_template(
        self, template_id: str, version: int | None = None
    ) -> dict[str, Any] | None:
        """按 template_id 定位模板。

        version=None → 返回最新版本；否则精确返回指定版本。无记录返回 None。
        """
        db = self._require_db()
        if version is not None:
            cursor = await db.execute(
                "SELECT * FROM prompt_templates WHERE template_id = ? AND version = ?",
                (template_id, version),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM prompt_templates WHERE template_id = ? ORDER BY version DESC LIMIT 1",
                (template_id,),
            )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_template_versions(self, template_id: str) -> list[dict[str, Any]]:
        """返回该模板的全部历史版本，按版本号升序。"""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT template_id, name, content, version, updated_at "
            "FROM prompt_templates WHERE template_id = ? ORDER BY version ASC",
            (template_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete_template(self, template_id: str, version: int | None = None) -> None:
        """删除模板。version=None → 删除所有版本；否则仅删除指定版本。"""
        db = self._require_db()
        if version is not None:
            await db.execute(
                "DELETE FROM prompt_templates WHERE template_id = ? AND version = ?",
                (template_id, version),
            )
        else:
            await db.execute(
                "DELETE FROM prompt_templates WHERE template_id = ?",
                (template_id,),
            )
        await db.commit()
