"""环境自检：解释器、依赖版本、关键模块导入与配置摘要（不打印任何密钥）。

用法：
    python scripts/check_env.py
退出码：
    0 通过；2 失败
"""

from __future__ import annotations

import importlib
import os
import sys
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag_core.config import RagError, load_settings

CORE_DISTRIBUTIONS = [
    "llama-index-core",
    "llama-index-llms-openai",
    "llama-index-embeddings-openai",
    "llama-index-retrievers-bm25",
    "bm25s",
    "jieba",
    "python-dotenv",
]

LOCAL_EMBED_DISTRIBUTIONS = [
    "llama-index-embeddings-huggingface",
    "torch",
    "sentence-transformers",
]

CORE_MODULES = [
    "llama_index.core",
    "llama_index.llms.openai",
    "llama_index.embeddings.openai",
    "llama_index.retrievers.bm25",
    "bm25s",
    "jieba",
]

LOCAL_EMBED_MODULES = [
    "llama_index.embeddings.huggingface",
    "torch",
    "sentence_transformers",
]


def _version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "<未安装>"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    failed = False

    print("=" * 60)
    print("环境自检")
    print("=" * 60)
    print(f"python      : {sys.version.split()[0]}")
    print(f"executable  : {sys.executable}")
    print(f"virtualenv  : {sys.prefix}")

    settings = None
    print("\n配置摘要（不含密钥）:")
    try:
        settings = load_settings("embed")
    except RagError as exc:
        failed = True
        print(f"  [FAIL] {exc.code}: {exc.reason}")
        if exc.hint:
            print(f"         {exc.hint}")
    else:
        for line in settings.safe_summary():
            print(f"  {line}")
        print(f"  data_dir    = {settings.data_dir}")
        print(f"  storage_dir = {settings.storage_dir}")
        primary_ready = settings.llm_primary.available()
        fallback_ready = settings.llm_fallback.available()
        primary_state = "已配置" if primary_ready else f"缺失 {settings.llm_primary.api_key_env}"
        fallback_state = "已配置" if fallback_ready else f"缺失 {settings.llm_fallback.api_key_env}"
        print(f"  llm_primary_key  = {primary_state}")
        print(f"  llm_fallback_key = {fallback_state}")
        if not primary_ready:
            if fallback_ready:
                print("  [WARN] 主模型 key 缺失：step2/step3 的启动预检仍会拒绝（主备兜底未覆盖此场景）")
            else:
                print("  [WARN] 主/备 LLM key 均缺失：step1 可运行，step2/step3 会失败")

    # 本地 Embedding 依赖只在 local 后端检查；cloud 后端不需要 torch/transformers。
    backend = settings.embed_backend if settings else os.getenv("RAG_EMBED_BACKEND", "").strip().lower()
    backend = backend or "local"
    local = backend == "local"
    distributions = CORE_DISTRIBUTIONS + (LOCAL_EMBED_DISTRIBUTIONS if local else [])
    modules = CORE_MODULES + (LOCAL_EMBED_MODULES if local else [])

    print(f"\n依赖版本（{backend} 后端）:")
    for name in distributions:
        version = _version(name)
        mark = "OK " if version != "<未安装>" else "FAIL"
        failed = failed or version == "<未安装>"
        print(f"  [{mark}] {name:<38} {version}")

    print("\n模块导入:")
    for module in modules:
        try:
            importlib.import_module(module)
            print(f"  [OK ] {module}")
        except Exception as exc:
            failed = True
            print(f"  [FAIL] {module} -> {type(exc).__name__}: {exc}")

    print("")
    if failed:
        print("环境自检未通过：请按上面 FAIL 项处理。")
        return 2
    print("环境自检通过：可以运行 scripts/step1_build_index.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
