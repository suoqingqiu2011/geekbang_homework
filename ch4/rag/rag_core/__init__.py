"""LlamaIndex 向量 + 中文 BM25 + RRF 混合检索 RAG 核心包。"""

from __future__ import annotations

import warnings

try:
    from pydantic.warnings import UnsupportedFieldAttributeWarning
except ImportError:  # pydantic < 2.11 不存在该告警类
    pass
else:
    # llama-index-core 0.12.x 在 Annotated 别名中使用了 Field(validate_default=True)，
    # pydantic >= 2.11 会提示该属性在此处无效（上游 0.14 已改为 Field()）。
    # 本工程锁定 0.12 系列，故只屏蔽这一条第三方告警。
    warnings.filterwarnings("ignore", category=UnsupportedFieldAttributeWarning)
