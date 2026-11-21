"""配置加载、路径常量与统一错误码。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STORAGE_DIR = ROOT / "storage"
OUTPUT_DIR = ROOT / "output"
ENV_FILE = ROOT / ".env"

ALLOWED_SUFFIXES = frozenset({".md", ".ts"})
EXCLUDED_DIRS = frozenset({"node_modules", "dist", ".git", ".venv", "__pycache__", "storage", "output"})
DEFAULT_QUESTION = "登录会话的有效期是多少分钟？"

EXIT_CODES = {
    "RAG-E01": 2,
    "RAG-E02": 4,
    "RAG-E03": 2,
    "RAG-E04": 4,
    "RAG-E05": 3,
    "RAG-E06": 3,
    "RAG-E07": 5,
}


class RagError(Exception):
    def __init__(self, code: str, reason: str, where: str = "", hint: str = "") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.where = where
        self.hint = hint
        self.exit_code = EXIT_CODES.get(code, 1)

    def render(self) -> str:
        parts = [f"[EXIT {self.exit_code}] {self.code} @ {self.where}", f"  原因: {self.reason}"]
        if self.hint:
            parts.append(f"  建议: {self.hint}")
        return "\n".join(parts)


@dataclass(frozen=True)
class LLMConfig:
    name: str
    base_url: str
    model: str
    api_key_env: str

    def available(self) -> bool:
        return bool(os.getenv(self.api_key_env, "").strip())

    def api_key(self) -> str:
        return require_env(self.api_key_env)


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    storage_dir: Path
    output_dir: Path
    embed_backend: str
    embed_model: str
    embed_dim: int
    embed_key_env: str
    embed_base_url: str
    chunk_size: int
    chunk_overlap: int
    top_k: int
    candidate_k: int
    rrf_k: float
    hybrid_weights: tuple[float, float]
    index_keep: int
    llm_primary: LLMConfig
    llm_fallback: LLMConfig
    question: str

    def safe_summary(self) -> list[str]:
        if not self.embed_key_env:
            key_state = "本地模型，无需 key"
        elif os.getenv(self.embed_key_env, "").strip():
            key_state = f"已配置 {self.embed_key_env}"
        else:
            key_state = f"缺失 {self.embed_key_env}"
        return [
            f"资料目录      = {self.data_dir}",
            f"embed_backend = {self.embed_backend} ({key_state})",
            f"embed_model   = {self.embed_model} / {self.embed_dim}d",
            f"llm_primary   = {self.llm_primary.model} @ {self.llm_primary.base_url}",
            f"llm_fallback  = {self.llm_fallback.model} @ {self.llm_fallback.base_url}",
            f"chunk         = {self.chunk_size} tokens / overlap {self.chunk_overlap}",
            f"top_k/cand    = {self.top_k} / {self.candidate_k}",
            f"rrf           = k={self.rrf_k:g}, 权重 vector={self.hybrid_weights[0]:g} / bm25={self.hybrid_weights[1]:g}",
            f"index_keep    = {self.index_keep}（0=保留全部历史版本）",
        ]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RagError("RAG-E03", f"环境变量 {name} 必须为整数", "config._env_int", f"检查 {ENV_FILE}") from None


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise RagError("RAG-E03", f"环境变量 {name} 必须为数字", "config._env_float", f"检查 {ENV_FILE}") from None


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RagError("RAG-E03", f"缺少必需环境变量 {name}", "config.require_env", f"在 {ENV_FILE} 中填写 {name}")
    return value


def load_settings(require: Literal["embed", "llm", "all"] = "all") -> Settings:
    load_dotenv(dotenv_path=ENV_FILE, override=False)

    backend = (_env("RAG_EMBED_BACKEND", "local") or "local").lower()
    if backend not in {"cloud", "local"}:
        raise RagError("RAG-E03", "RAG_EMBED_BACKEND 只能是 cloud 或 local", "config.load_settings", f"检查 {ENV_FILE}")

    if backend == "cloud":
        embed_model = _env("RAG_EMBED_MODEL", "text-embedding-v3")
        embed_dim = _env_int("RAG_EMBED_DIM", 1024)
        embed_key_env = "DASHSCOPE_API_KEY"
        embed_base_url = _env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    else:
        embed_model = _env("LOCAL_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
        embed_dim = _env_int("LOCAL_EMBED_DIM", 512)
        embed_key_env = ""
        embed_base_url = ""

    chunk_size = _env_int("RAG_CHUNK_SIZE", 384)
    chunk_overlap = _env_int("RAG_CHUNK_OVERLAP", 76)
    if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise RagError("RAG-E03", "分块参数非法：需满足 0 <= overlap < size", "config.load_settings", f"检查 {ENV_FILE}")

    top_k = _env_int("RAG_TOP_K", 5)
    candidate_k = _env_int("RAG_CANDIDATE_K", 20)
    if top_k <= 0 or candidate_k < top_k:
        raise RagError("RAG-E03", "检索参数非法：需满足 0 < RAG_TOP_K <= RAG_CANDIDATE_K", "config.load_settings", f"检查 {ENV_FILE}")

    rrf_k = _env_float("RAG_RRF_K", 60.0)
    vector_weight = _env_float("RAG_HYBRID_VECTOR_WEIGHT", 1.0)
    bm25_weight = _env_float("RAG_HYBRID_BM25_WEIGHT", 1.0)
    if rrf_k <= 0:
        raise RagError("RAG-E03", "RAG_RRF_K 必须大于 0", "config.load_settings", f"检查 {ENV_FILE}")
    if vector_weight < 0 or bm25_weight < 0 or vector_weight + bm25_weight <= 0:
        raise RagError(
            "RAG-E03", "RAG_HYBRID_*_WEIGHT 必须非负且不能同时为 0",
            "config.load_settings", f"检查 {ENV_FILE}",
        )

    index_keep = _env_int("RAG_INDEX_KEEP", 3)
    if index_keep < 0:
        raise RagError("RAG-E03", "RAG_INDEX_KEEP 不能为负数", "config.load_settings", f"检查 {ENV_FILE}")

    primary = LLMConfig(
        "primary",
        _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        _env("RAG_LLM_MODEL", "deepseek-chat"),
        "DEEPSEEK_API_KEY",
    )
    fallback = LLMConfig(
        "fallback",
        _env("QWEN_FALLBACK_BASE_URL", "https://portal.qwen.ai/v1"),
        _env("QWEN_FALLBACK_MODEL", "qwen3-coder-plus"),
        "QWEN_FALLBACK_API_KEY",
    )

    settings = Settings(
        root=ROOT,
        data_dir=DATA_DIR,
        storage_dir=STORAGE_DIR,
        output_dir=OUTPUT_DIR,
        embed_backend=backend,
        embed_model=embed_model,
        embed_dim=embed_dim,
        embed_key_env=embed_key_env,
        embed_base_url=embed_base_url,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        top_k=top_k,
        candidate_k=candidate_k,
        rrf_k=rrf_k,
        hybrid_weights=(vector_weight, bm25_weight),
        index_keep=index_keep,
        llm_primary=primary,
        llm_fallback=fallback,
        question=_env("RAG_QUESTION", DEFAULT_QUESTION) or DEFAULT_QUESTION,
    )

    if require in ("embed", "all") and settings.embed_key_env:
        require_env(settings.embed_key_env)
    if require in ("llm", "all"):
        require_env(primary.api_key_env)

    return settings
