# 基于 LlamaIndex 的向量与关键词混合检索 RAG 规格计划书

| 项 | 内容 |
|---|---|
| 文档编号 | SPEC_rag |
| 版本 | 1.0.0 |
| 状态 | 已实现并验收（grill-me 三轮确认 + 课程文档对照分析后定稿；代码已落盘，step1/2/3 已运行） |
| 适用对象 | 作业四第二题（`docs/rag/q4-2.md`）：基于 LlamaIndex 实现向量与关键词混合检索 RAG |
| 代码位置 | `rag/`（Python 3.12 + LlamaIndex 0.12 系列） |
| 关联文档 | `docs/rag/q4-2.md`（作业要求）、`login_demo/artifacts/login-flow.md`（资料真源）、`docs/第四章_知识点总结与逻辑框架.md`（课程 §4.4 RAG / §4.6 接入要求）、`pi-ltm/docs/架构说明.md`（既有工程规范参照） |
| 更新日期 | 2026-11-21 |

---

## 1. 问题陈述（Problem Statement）

`login_demo` 登录会话项目的资料会越来越多（README、登录调用链说明、11 个 TypeScript 源文件）。当用户提问"登录会话的有效期是多少分钟？"时，Agent 需要：

1. 从多份资料中**找到回答所需的证据**（分块 + 建索引）；
2. 对比**向量检索、BM25 关键词检索、RRF 混合检索**三种方式的效果；
3. 用混合检索证据交给真实模型生成**带来源引用的回答**；
4. 当资料更新（有效期 15 → 20 分钟）后，**同步更新两路索引**，确保旧分块退出当前检索，回答与引用改用新资料。

**前置事实缺口**：现有 `login-flow.md` 与全部源码中均无"会话有效期"字段，无法检索到答案。经三轮评审确认：在资料中新增 §6「会话有效期」小节（初始值 **15 分钟**），并同步写入原始资料与作业副本，第三步仅在副本中改为 20 分钟。

---

## 2. 方案概述（Solution）

以 **Python + LlamaIndex 0.12** 构建本地 RAG 流水线：

- **离线准备**：读取 `rag/data/**` 语料副本（`.md`/`.ts`，排除 `node_modules/dist/.git/.venv/__pycache__`）→ 分块（`SentenceSplitter`，chunk_size=384 / overlap=76）→ 回填章节与行号元数据 → 同一批节点建立**向量索引**（`SimpleVectorStore`）与**中文 BM25 索引**（`ChineseBM25Retriever`，jieba 分词）→ 双路落盘到版本化目录 `storage/index-<hash8>/`；
- **在线回答**：向量 / BM25 / RRF 三路分别检索同一问题（候选深度 20，展示前 5）→ RRF 混合证据（`QueryFusionRetriever`，`RECIPROCAL_RANK`，不启用查询改写）→ 带 `[n]` 引用约束提示词 → DeepSeek `deepseek-chat`（故障时 Qwen `qwen3-coder-plus` 兜底）生成带引用回答；
- **更新验证**：改写副本中有效期 15 → 20 分钟 → 全量重建双路索引 → 断言旧分块退出（A1–A8）→ 重新提问确认回答与引用使用新资料；
- **可运维**：三道防线（启动预检 / 运行时重试与原子提交 / 后置断言）、统一错误码 `RAG-E01..E07`、stdout 与 `output/*.md` 同源输出（stderr 仅诊断）、双 Embedding 后端（cloud=DashScope `text-embedding-v3` 1024 维 / local=BGE `bge-small-zh-v1.5` 512 维）可切换。

---

## 3. 目标与非目标

### 3.1 目标（对应 q4-2.md 三步）

| 编号 | 目标 |
|---|---|
| G1 | 读取资料并分块，基于**同一批节点**建立向量索引与 BM25 关键词索引，使用真实 Embedding，**保存索引**，输出节点数量及一条带来源的分块样例 |
| G2 | 分别用向量、BM25、RRF 混合检索查询"登录会话的有效期是多少分钟？"，输出各自前 5 条结果及来源；将混合证据交给真实模型生成带来源引用（`[n]`）的回答 |
| G3 | 将资料中的登录有效期改为 20 分钟，**同步更新两路索引**，确保旧分块退出当前检索；重新查询同一问题，输出回答及引用的新资料 |

### 3.2 非目标（Out of Scope 摘要，详见 §17）

- 不接 Agent / 工具运行时（第二题独立验证 RAG 后端，对应课程 §4.6"先独立验证"）；
- 不实现 Rerank、评估集与召回率/命中率统计、Metadata Filter、查询改写/澄清；
- 不接入 Elasticsearch、向量数据库外置服务；不实现多项目隔离与权限模型；
- 不修改 `login_demo` 源码（仅补资料文档）。

---

## 4. 用户故事（User Stories）

1. 作为作业执行者，我希望一键复制 `login_demo` 资料到 `rag/data/` 副本，以便第三步改写时不影响原始资料。
2. 作为作业执行者，我希望原始资料与副本都包含「会话有效期 15 分钟」小节，以便检索有据可依且更新有明确旧值。
3. 作为作业执行者，我希望建库脚本扫描语料时只收 `.md/.ts` 并排除 `node_modules/dist` 等目录，以便证据不被第三方库污染。
4. 作为作业执行者，我希望每个分块节点带来源、章节、起止行号与内容哈希，以便回答可以回源核对。
5. 作为作业执行者，我希望向量索引与 BM25 索引基于同一批节点构建，以便混合检索两路范围一致。
6. 作为作业执行者，我希望 BM25 使用 jieba 中文分词且加载时校验分词标记，以便中文关键词检索可用且不会被静默降级。
7. 作为作业执行者，我希望两路索引都持久化到磁盘，以便满足题目"保存索引/更新两路索引"要求。
8. 作为作业执行者，我希望建库过程先写入临时目录、自检通过后再原子切换指针，以便失败时不产生半成品索引。
9. 作为作业执行者，我希望索引目录携带 manifest（Embedding 配置、分块参数、语料指纹、文件清单），以便换后端/改资料时能被显式拒绝而非静默出错。
10. 作为作业执行者，我希望建库脚本输出节点总数与一条含来源的分块样例，以便截图提交第一步。
11. 作为作业执行者，我希望三种检索方式各自输出 Top-5 结果（含分数语义与来源），以便对比三路效果。
12. 作为作业执行者，我希望混合检索结果标注"该节点被哪些路召回及排名"，以便解释 RRF 融合依据。
13. 作为作业执行者，我希望回答的每个事实陈述都带 `[n]` 引用编号，以便核对证据来源。
14. 作为作业执行者，我希望引用编号经过合法性校验（越界编号被标出），以便不虚报可追溯性。
15. 作为作业执行者，我希望查询阶段默认对缺失/损坏/过期索引快速失败，以便不悄悄改变对比对象。
16. 作为作业执行者，我希望第三步改写副本后自动重建双路索引，以便旧分块退出当前检索。
17. 作为作业执行者，我希望第三步输出 A1–A8 断言结果（旧值退出、新值进入、引用指向新分块），以便确认更新同步。
18. 作为作业执行者，我希望第三步幂等可重跑（副本已是 20 分钟时跳过基线对照），并支持 `--restore` 回到 15 分钟，以便重复演示不报错。
19. 作为作业执行者，我希望所有失败输出稳定错误码 + 原因 + 位置 + 建议，且永不回显密钥值，以便快速定位问题。
20. 作为作业执行者，我希望 stdout 与 `output/*.md` 文件内容一致（stderr 仅承载诊断，不写入报告），以便截图与复核同源。
21. 作为作业执行者，我希望支持 `RAG_EMBED_BACKEND=cloud|local` 切换 Embedding 后端，以便无百炼 key 时用本地 BGE 完成作业。
22. 作为作业执行者，我希望 LLM 主备自动切换（DeepSeek 失败时用 Qwen），以便回答环节不因单一服务故障中断。

---

## 5. 系统架构概述

### 5.1 架构总览

```
┌────────────────────────────────────────────────────────────────┐
│ scripts/step1_build_index.py  建库入口                          │
│ scripts/step2_compare_query.py 三路对比 + 混合回答              │
│ scripts/step3_update_verify.py 更新 + 重建 + 断言               │
└───────────────┬────────────────────────────────────────────────┘
                │ 导入 rag_core（统一异常 RagError → 退出码）
┌───────────────▼────────────────────────────────────────────────┐
│ rag_core/                                                       │
│  config.py      .env 加载、Settings、错误码表、路径常量          │
│  providers.py   get_embed_model() / get_llm() / 主备回答        │
│  corpus.py      data/** → Document → TextNode（行号/章节/hash） │
│  indexer.py     双路建库、版本化原子提交、manifest、加载校验      │
│  retrieval.py   向量 / 中文BM25 / RRF 三路 + 排名表             │
│  answering.py   引用约束提示词、LLM 调用、引用解析               │
│  formatting.py  Tee 同源输出、来源标签、表格打印                 │
└───────┬──────────────────────┬─────────────────────────────────┘
        │                      │
┌───────▼─────────┐   ┌────────▼──────────────────────────────────┐
│ storage/        │   │ data/（资料副本，唯一检索语料）             │
│ current.json    │   │  artifacts/login-flow.md（含 §6 15分钟）   │
│ index-<hash8>/  │   │  README_login_demo.md                     │
│   vector/       │   │  src/**/*.ts（11 个源文件）                │
│   bm25/         │   └────────┬──────────────────────────────────┘
│   manifest.json │            │ 第三步改写 §6：15 → 20 分钟
└─────────────────┘            ▼
                         全量重建 → 新 index-<hash8'> + current.json 切换
```

### 5.2 分层职责

| 层 | 模块 | 职责 | 依赖 |
|---|---|---|---|
| 入口层 | `scripts/step1..3` | 三步作业编排、报告输出、退出码映射 | `rag_core` |
| 配置层 | `rag_core/config.py` | `.env` 加载与校验、Settings、`RagError`（错误码/退出码）、路径常量 | `python-dotenv` |
| 提供层 | `rag_core/providers.py` | Embedding/LLM 构造、指数退避重试、LLM 主备切换 | LlamaIndex OpenAI 系集成包 |
| 语料层 | `rag_core/corpus.py` | 语料扫描/指纹、分块、行号与章节回填、chunk_hash | `SentenceSplitter` |
| 索引层 | `rag_core/indexer.py` | 双路建库、临时目录原子提交、`current.json` 指针、manifest、加载与一致性校验 | `SimpleVectorStore`、`ChineseBM25Retriever` |
| 检索层 | `rag_core/retrieval.py` | 三路检索器构造、RRF 融合、多路命中映射、排名行 | `VectorIndexRetriever`、`QueryFusionRetriever`、`bm25s` |
| 回答层 | `rag_core/answering.py` | 引用约束提示词、LLM 回答、引用编号解析与越界检测 | `providers`、`formatting` |
| 输出层 | `rag_core/formatting.py` | 屏幕+文件同源输出（Tee）、来源标签、表格 | 标准库 |

### 5.3 依赖方向与约束

- 依赖方向**严格单向**：`scripts → rag_core.*`；模块间 `answering → providers/formatting/retrieval`、`indexer → corpus/retrieval/config`、`retrieval → config`；
- 全部第三方依赖走 **纯 Python 或官方 wheel**，避开本地编译（torch 为可选项，独立 requirements 文件）；
- 版本锁定 LlamaIndex **0.12 系列**（core `~=0.12.44`、retrievers-bm25 `==0.5.2`，经 `.probe/` 源码核验 API）；
- BM25 分词**固定 jieba**，禁止静默回退英文词干；Embedding 后端切换后**必须重建索引**（维度不同不可互换）。

### 5.4 技术栈

| 项 | 选型 | 约束来源 |
|---|---|---|
| 语言/运行时 | Python 3.12（独立 `.venv`） | 用户要求；避开 3.13+ 生态风险 |
| RAG 框架 | LlamaIndex 0.12 系列（core/llms-openai/embeddings-openai/retrievers-bm25） | 作业要求 + 版本锁定 |
| 向量存储 | `SimpleVectorStore`（本地 JSON，`storage_context.persist`） | 作业要求"保存索引" |
| 关键词索引 | 官方 `BM25Retriever` 的中文适配子类 `ChineseBM25Retriever`（bm25s 引擎 + jieba） | 中文分词契约 + 0.5.2 源码核验 |
| 混合检索 | `QueryFusionRetriever`，`RECIPROCAL_RANK`（RRF），`num_queries=1` 关闭查询改写 | 作业要求 RRF + 三路同问题对比 |
| Embedding | cloud：DashScope `text-embedding-v3`（1024 维，OpenAI 兼容协议）；local：`BAAI/bge-small-zh-v1.5`（512 维，CPU） | 用户指定主/保底方案 |
| 生成模型 | 主：DeepSeek `deepseek-chat`；备：Qwen Token Plan `qwen3-coder-plus`（仅故障时） | 用户指定 |
| 分词 | jieba（BM25 唯一分词器） | 中文 BM25 一致分词要求 |
| 持久化 | 版本化目录 `storage/index-<hash8>/` + 单文件指针 `current.json`（`os.replace` 原子写） | Windows 目录替换非原子约束 |
| 输出 | `print` + `output/*.md` 同源（Tee），不引入日志框架 | 用户 Q9 要求 |

### 5.5 目录结构

```
rag/
├── .env / .env.example / .gitignore
├── requirements.txt                 # 核心（纯 wheel）
├── requirements-local-embed.txt     # 可选：torch + HF embedding
├── README.md                        # 运行命令与前置条件
├── data/                            # 资料副本（唯一检索语料）
│   ├── artifacts/login-flow.md      # 含 §6 会话有效期（初始 15 分钟）
│   ├── README_login_demo.md
│   └── src/**/*.ts
├── storage/                         # 索引产物（可删可重建）
│   ├── .baseline/login-flow.md      # step3 的 15 分钟基线快照（--restore 使用）
│   ├── current.json                 # {"dir": "index-<hash8>", "built_at": ...}
│   └── index-<hash8>/
│       ├── vector/                  # SimpleVectorStore + docstore + index_store
│       ├── bm25/                    # bm25s 引擎 + retriever.json（tokenizer=jieba）
│       └── manifest.json
├── output/                          # step1/2/3 的 .md 证据
├── rag_core/
│   ├── __init__.py
│   ├── config.py / providers.py / corpus.py
│   ├── indexer.py / retrieval.py
│   └── answering.py / formatting.py
└── scripts/
    ├── step1_build_index.py
    ├── step2_compare_query.py
    └── step3_update_verify.py
```

---

## 6. 核心功能模块说明

### 6.1 `config.py`（配置与错误模型）

- 从 `rag/.env` 加载分层环境变量（`load_dotenv(override=False)`）；
- 校验项：`RAG_EMBED_BACKEND ∈ {cloud, local}`、分块参数 `0 <= overlap < size`、检索参数 `0 < top_k <= candidate_k`、必需 key 存在性（**只报变量名，永不回显值**）；
- `RagError`：携带稳定错误码 `RAG-E01..E07`、退出码、原因、位置、建议；`render()` 输出不含任何密钥；
- `Settings`：`data_dir / storage_dir / output_dir / embed_backend / embed_model / embed_dim / chunk_size / chunk_overlap / top_k / candidate_k / llm_primary / llm_fallback / question`；`safe_summary()` 供报告打印脱敏配置。

### 6.2 `providers.py`（模型提供层）

- `get_embed_model()`：cloud 走 `OpenAIEmbedding`（`api_base=settings.embed_base_url`、`dimensions=embed_dim`、batch=10）；local 走 `HuggingFaceEmbedding`（CPU、`max_length=512`、batch=16、query/text instruction 区分；支持 `HF_ENDPOINT` 镜像）；
- `get_llm()`：`OpenAI`（OpenAI 兼容协议，`temperature=0.1`、`max_tokens=768`）；
- `complete_with_fallback()`：主备模型顺序调用，每模型指数退避重试 3 次（仅幂等调用）；全部失败抛 `RAG-E06`。

### 6.3 `corpus.py`（语料与分块）

- `iter_source_files()`：递归扫描 `data/`，仅 `.md/.ts`，排除 `node_modules/dist/.git/.venv/__pycache__/storage/output`；
- `corpus_fingerprint()`：全部文件 `sha256` 的聚合指纹（索引陈旧检测依据）；
- `build_corpus()`：
  - `Document(text, metadata={source, file_sha256})` → `SentenceSplitter(384/76)` → `TextNode`；
  - 行号回填：自建 `_line_starts` 二分映射 + 单调递增 `find` 游标（不依赖 splitter 内部偏移字段）+ `strip` 前后空白补偿；
  - 章节回填：md 用 `#` 标题正则、ts 用顶层声明正则（`export function/class/interface/const…`），命中节点记录最近标题；
  - 元数据：`source / file_sha256 / section / start_line / end_line / chunk_hash / chunk_index`；
  - 确定性 `node_id = "{source}:{start_line}-{end_line}:{chunk_hash}:{index}"`；
  - tiktoken 词表不可用时降级为字符计数并在 stderr 提示（不中断建库）；
- `Corpus`：`nodes / files（FileEntry: path/sha256/bytes/lines）/ sha256 / total_chars`。

### 6.4 `indexer.py`（双路索引与版本管理）

- `build_index()`：
  1. 计算 `_core_manifest`（embed + chunk + bm25 配置 + corpus 指纹）→ `index_id = sha256(manifest)[:8]`；
  2. 全部产物先写入 `storage/.tmp-<id>-<ts>/`：`vector/`（`index.storage_context.persist`）与 `bm25/`（`ChineseBM25Retriever.persist`）+ `manifest.json`；
  3. `_verify_persisted()` 落盘自检：重新加载双路索引并核对节点数（与建库一致才允许提交）；
  4. 成功 → `os.replace(tmp_dir, index-<id>)` → 原子写 `current.json` 指针；失败 → 清理临时目录、保留旧索引、抛 `RAG-E05`；
  5. 同 id 目录已存在 → 幂等复用（保留原 built_at），指针重指；
- `load_index()`：读指针 → 读 manifest → `_check_compat`（embed 后端/模型/维度、分块参数不一致 → `RAG-E02`）→ 语料指纹不一致且未 `--allow-stale` → `RAG-E04`（默认快速失败，不自动重建）→ 加载向量 + BM25；
- `LoadedIndex`：`index / nodes / bm25 / manifest / index_dir`；
- `bm25_node_ids()`：取 BM25 corpus 的节点 id 集合（供 step1 断言"同一批节点"）。

### 6.5 `retrieval.py`（三路检索与中文 BM25）

**`ChineseBM25Retriever`（官方 `BM25Retriever` 子类，四条契约）**：

| 契约 | 内容 |
|---|---|
| a | 语料侧/查询侧使用同一套 jieba 分词 + `stopwords=None`/`stemmer=None`/`token_pattern`；覆写 `_retrieve` 显式传参，避免查询侧回退默认 `english` 停用词 |
| a′ | 覆写公开入口 `retrieve()`：对查询做同构 jieba 分词后再调 `super().retrieve()`（官方 `_retrieve` 内部用默认切分器，不覆写入口则查询侧 token 与语料侧不重合，BM25 失效） |
| b | 覆写 `persist` / `from_persist_dir`：落盘 `retriever.json` 写入 `"tokenizer": "jieba"` 标记 |
| c | 加载时标记不是 `jieba` → 立即 `RAG-E02`，**不降级** |
| d | jieba 缺失 → 构造/加载入口立即 `RAG-E01`，**绝不静默回退默认分词** |

- `build_vector_retriever()`：`VectorIndexRetriever(similarity_top_k=candidate_k)`；
- `build_bm25_retriever()`：`ChineseBM25Retriever(nodes, similarity_top_k=candidate_k)`；
- `load_bm25_retriever()`：从 `index-*/bm25/` 加载并校验标记；
- `build_hybrid_retriever()`：`QueryFusionRetriever(retrievers=[vector, bm25], llm=显式主模型, similarity_top_k=top_k, num_queries=1, mode=RECIPROCAL_RANK, retriever_weights=[1.0, 1.0], use_async=False)`——候选深度 20 融合后取前 5；零分候选先过滤并按剩余名次重排，融合键统一为 `node_id`；显式传 `llm` 以规避框架回退 `Settings.llm` 的隐式依赖；
- `hit_map()/to_rows()/route_overlap()`：多路命中标记（`vec#2, bm25#1`）、`Row(rank/score/kind/node/hits)` 排名行、混合命中构成统计。

### 6.6 `answering.py`（引用约束回答）

- `build_prompt()`：系统规则（仅依据证据、首行直接回答、事实陈述后必带 `[n]`、禁止编造编号、证据不足输出「证据不足：…」）+ 编号证据块（含 `src_label` 来源）+ 问题；
- `parse_citations()`：正则解析 `[n]`（负向后顾避免误吞 Markdown 链接），区分有效编号与越界编号；
- `answer()`：主备模型生成 → `AnswerResult(text/model/cited/invalid/prompt)`；`citation_ok = 有有效引用且无越界`；
- `render_answer()`：step2/step3 共用的回答与引用校验渲染（标题、回答、`[n]` 来源、越界编号、结论）；
- 引用三层校验策略：**第一层（编号合法）通用实现**；第二层（实体核对）/第三层（结论支持）本版仅在本作业的固定问题与 step3 断言中实现（见 §18 已知限制 D1）。

### 6.7 `formatting.py`（同源输出）

- `Tee`：同一文本同时写入 stdout 与 `output/*.md`（UTF-8、`\n`、flush），保证截图与复核文件逐字一致；stderr 仅用于重试/降级诊断，不写入报告；
- `ensure_utf8_stdio()`：Windows 下 stdout/stderr `reconfigure(encoding="utf-8")`，避免中文乱码；
- `src_ref()/src_label()`：`"{source} L{start}-L{end} {section}"` 来源标签；`preview()` 120 字展示截断（仅展示层，不进 prompt）；`print_rows()` 排名表（含分数语义与多路命中）。

### 6.8 `scripts/`（三步入口）

| 脚本 | 职责 | 产物 |
|---|---|---|
| `step1_build_index.py` | `[1/5]` 语料扫描 → `[2/5]` 分块与样例 → `[3/5]` Embedding → `[4/5]` 双路落盘与自检（同一批节点集合一致、`loaded.bm25` Top-1 自检；不一致 → `RAG-E05`）→ `[5/5]` 完成 | `output/step1_build_index.md` |
| `step2_compare_query.py` | 三路 Top-5 对比（分数语义 + 多路命中）→ 混合证据 → 带引用回答 → 引用校验；自检 hybrid `node_id` 唯一、默认问题双路命中 > 0（失败 → `RAG-E07`）；参数 `--question / --allow-stale / --auto-build`（默认均关） | `output/step2_compare_query.md` |
| `step3_update_verify.py` | `[0]` 前置检查 §6 并保存 15 分钟基线 → `[A]` 更新前基线 → `[B]` 改写 §6 15→20 分钟（显示改写前后与 sha256）→ `[C]` 全量重建双路索引 → `[D]` 断言 A1–A8（A2/A4 限定 §6）→ `[E]` 重新回答与引用核对；`--restore` 恢复 15 分钟并校验 R1–R3；幂等可重跑 | `output/step3_update_verify.md` / `output/step3_restore.md` |

---

## 7. 接口定义

### 7.1 config.py

```python
class RagError(Exception):
    def __init__(self, code: str, reason: str, where: str = "", hint: str = "")
    # code ∈ {"RAG-E01".."RAG-E07"}；exit_code 由 EXIT_CODES 映射
    def render(self) -> str          # "[EXIT n] CODE @ where\n  原因…\n  建议…"（不含密钥）

@dataclass(frozen=True)
class Settings:
    root / data_dir / storage_dir / output_dir: Path
    embed_backend / embed_model: str; embed_dim / chunk_size / chunk_overlap: int
    top_k / candidate_k: int; llm_primary / llm_fallback: LLMConfig
    question: str
    def safe_summary(self) -> list[str]

def load_settings(require: Literal["embed", "llm", "all"] = "all") -> Settings
def require_env(name: str) -> str     # 缺失 → RAG-E03（只报变量名）
```

### 7.2 providers.py

```python
def get_embed_model(settings: Settings) -> Any        # OpenAIEmbedding | HuggingFaceEmbedding
def get_llm(config: LLMConfig) -> Any   # llama_index.llms.openai.OpenAI
def complete_with_fallback(settings: Settings, prompt: str) -> tuple[str, str]  # (text, model)；全失败 → RAG-E06
```

### 7.3 corpus.py

```python
@dataclass(frozen=True) class FileEntry:  path / sha256 / bytes / lines
@dataclass class Corpus: nodes / files / sha256 / total_chars
def iter_source_files(data_dir: Path) -> list[Path]
def corpus_fingerprint(settings: Settings) -> str
def build_corpus(settings: Settings) -> Corpus    # 空语料/空分块 → RAG-E03
```

### 7.4 indexer.py

```python
@dataclass class LoadedIndex: index / nodes / bm25 / manifest / index_dir
def bm25_node_ids(loaded: LoadedIndex) -> set[str]
def build_index(settings, embed_model, corpus) -> tuple[str, Path]  # (index_id, index_dir)；失败 → RAG-E05
def load_index(settings, embed_model, allow_stale: bool = False) -> LoadedIndex
    # 缺失/损坏 → RAG-E04；配置不匹配 → RAG-E02；语料变更 → RAG-E04（陈旧）
def prune_indexes(storage_dir: Path, keep: int, current: str) -> int  # 保留最近 keep 个版本；keep<=0 不清理
```

### 7.5 retrieval.py

```python
class ChineseBM25Retriever(BM25Retriever):
    def __init__(self, nodes=None, existing_bm25=None, similarity_top_k=10, verbose=False, **kwargs)
    def retrieve(self, str_or_query_bundle) -> list[NodeWithScore]   # 同构 jieba 分词后走 _retrieve
    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]  # 显式传 stopwords/stemmer/token_pattern
    def persist(self, path: str, **kwargs) -> None                    # 落盘 tokenizer=jieba 标记
    @classmethod
    def from_persist_dir(cls, path: str, **kwargs) -> "ChineseBM25Retriever"  # 标记不符 → RAG-E02

def build_vector_retriever(index, settings) -> VectorIndexRetriever
def build_bm25_retriever(nodes, settings) -> ChineseBM25Retriever
def load_bm25_retriever(index_dir: Path, settings) -> ChineseBM25Retriever   # 目录缺失 → RAG-E04
def build_hybrid_retriever(index, bm25, settings, llm) -> QueryFusionRetriever
def hit_map(routes) -> dict[str, dict[str, int]]
def to_rows(results, kind, merged=None, limit=None) -> list[Row]
def route_overlap(merged, rows) -> dict[str, int]
```

### 7.6 answering.py / formatting.py

```python
@dataclass class AnswerResult: text / model / cited / invalid / prompt;  @property citation_ok
def build_prompt(question: str, rows: list[Row]) -> str
def parse_citations(text: str, evidence_count: int) -> tuple[list[int], list[int]]
def answer(settings, question: str, rows: list[Row]) -> AnswerResult
def render_answer(tee, result: AnswerResult, rows: list[Row], title="混合检索回答") -> None  # step2/step3 共用

class Tee:  __init__(path) / __call__(text="") / close()  # print + 文件同源（stdout）
def ensure_utf8_stdio() -> None
def src_ref(meta) -> str;  def src_label(meta) -> str
def print_rows(tee, title, rows, note="") -> None
```

### 7.7 数据契约

**`storage/index-<hash8>/manifest.json`**：

```json
{
  "schema_version": 1,
  "index_id": "index-a1b2c3d4",
  "built_at": "2026-11-21T10:00:00+08:00",
  "embed":  { "backend": "local", "model": "BAAI/bge-small-zh-v1.5", "dim": 512 },
  "chunk":  { "size": 384, "overlap": 76, "splitter": "SentenceSplitter" },
  "bm25":   { "tokenizer": "jieba", "stopwords": null, "stemmer": null },
  "corpus": { "sha256": "<语料聚合指纹>" },
  "nodes": 63,
  "files":  [ { "path": "artifacts/login-flow.md", "sha256": "…", "bytes": 5120, "lines": 190 } ]
}
```

**`storage/current.json`**：`{"dir": "index-<hash8>", "built_at": "<ISO8601>"}`（`os.replace` 原子替换）。

**`storage/index-*/bm25/retriever.json`**：`{"similarity_top_k": 20, "verbose": false, "tokenizer": "jieba"}`。

**节点元数据**：`source / file_sha256 / section / start_line / end_line / chunk_hash / chunk_index`。

---

## 8. 数据流设计

### 8.1 离线建库流（step1）

```
data/**（.md/.ts，13 文件）
  → corpus.iter_source_files（过滤目录/后缀）
  → corpus_fingerprint（全文件 sha256 聚合）
  → Document(text, {source, file_sha256})
  → SentenceSplitter(384/76)
  → TextNode（行号/章节/chunk_hash 回填，确定性 node_id）
  → VectorStoreIndex(nodes, embed_model).storage_context.persist(tmp/vector/)
  → ChineseBM25Retriever(nodes).persist(tmp/bm25/)   ← 同一批节点
  → manifest.json 写入 tmp/
  → _verify_persisted（双路重新加载，节点数核对）
  → os.replace(tmp → index-<hash8>)
  → current.json 原子重指
  → step1 自检：vector id 集合 == bm25 id 集合；BM25 对默认问题 Top-1 命中
```

### 8.2 在线查询流（step2）

```
问题 "登录会话的有效期是多少分钟？"
  → load_index（指针 → manifest → 兼容性/陈旧性校验）
  ├─ 向量路：VectorIndexRetriever.retrieve(question)          → 候选 20
  ├─ BM25 路：ChineseBM25Retriever.retrieve(question)          → 候选 20
  └─ 混合路：QueryFusionRetriever(RRF, 两路各 20)               → 融合后 Top-5
  → hit_map（多路命中标记）
  → 三路各打印 Top-5（分数语义 cos/bm25/rrf + 来源 + 章节 + 行号）
  → build_prompt(question, 混合 Top-5)
  → complete_with_fallback（DeepSeek 主 → Qwen 备，各重试 3）
  → parse_citations（越界编号检测）
  → 输出：回答 + 引用清单（编号 → 来源）+ citation_ok 结论
  → stdout 与 output/step2_compare_query.md 同源（stderr 为诊断）
```

### 8.3 更新验证流（step3）

```
[0] 前置检查 §6 是否存在且含 15/20 分钟（失败 → RAG-E03）；保存 15 分钟基线到 storage/.baseline/login-flow.md
[A] 读旧指针 → 旧索引混合检索默认问题 → 统计 §6 命中「15 分钟」节点数（软提示 A1）
[B] data/artifacts/login-flow.md：仅 §6 内 "15 分钟" → "20 分钟"（replace；0 处 → RAG-E05）
    记录改写前后 §6 文本与 sha256
[C] build_corpus + build_index → 新 index-<hash8'> → current.json 切换（旧目录保留供对照）
[D] 断言（A2/A4 限定 artifacts/login-flow.md 的 §6 节点）：
    A2 新索引 §6 无「15 分钟」          A3 新索引 §6 含「20 分钟」
    A4 Top-20 §6 无旧值                A5 Top-20 §6 有新值
    A6 落盘节点数 == 语料节点数
[E] 混合检索 → 回答：
    A7 回答含「20 分钟」（作业口径）     A8 引用节点为 §6 且含新值、不含旧值
全部通过 → 报告"旧分块已退出当前检索"；任一失败 → RAG-E07 / exit 5
```

`--restore`：读取 `storage/.baseline/login-flow.md`（必须含 §6 的 15 分钟）→ 覆盖副本 → 重建双路索引 → 校验 R1/R2/R3 → `output/step3_restore.md`；默认运行不自动恢复，最终保持 20 分钟。

---

## 9. 错误码与三道防线

### 9.1 错误码表

| 码 | 退出码 | 触发场景 | 防线 |
|---|---|---|---|
| RAG-E01 | 2 | jieba / bm25s / torch 依赖缺失（禁止回退默认分词） | 预检 |
| RAG-E02 | 4 | 索引不匹配：BM25 分词标记 ≠ jieba；Embedding 后端/模型/维度或分块参数与当前配置不一致 | 预检（加载时） |
| RAG-E03 | 2 | 环境变量缺失或非法（只报变量名）；资料目录缺失/空；缺少 §6 或 §6 无 15/20 分钟；分块或检索参数非法 | 预检 |
| RAG-E04 | 4 | 索引指针/目录/manifest 缺失或损坏；语料变更（陈旧） | 预检（加载时） |
| RAG-E05 | 3 | 建库失败（Embedding/网络/落盘）；§6 改写未命中；临时目录已清理、旧索引保留 | 运行时 |
| RAG-E06 | 3 | 主备 LLM 全部调用失败（重试后） | 运行时 |
| RAG-E07 | 5 | 后置断言失败（step2 融合自检 / step3 A2–A8 / restore R1–R3） | 后置 |

错误输出统一格式：`[EXIT n] CODE @ 位置 → 原因 → 建议`，永不回显密钥值。

### 9.2 三道防线

| 防线 | 措施 |
|---|---|
| 启动前预检 | 配置校验（RAG-E03）、依赖存在性（RAG-E01）、索引存在/可解析/配置一致/语料未变更（RAG-E02/E04）；查询默认快速失败，`--auto-build` 默认关闭 |
| 运行时 | 幂等调用指数退避重试（Embedding/LLM 各 3 次）；建库全部产物先入 `storage/.tmp-*`，自检通过后整体提交，失败清理临时目录并保留旧索引（RAG-E05） |
| 结果后置 | step1 落盘自检（双路节点数 + 集合一致 + BM25 Top-1）；step3 A1–A8 断言（RAG-E07） |

---

## 10. 性能指标与非功能需求

| 项 | 要求/设计值 | 说明 |
|---|---|---|
| 语料规模 | ~13 文件 / ~4 万字符 / 约 60–70 节点 | 单机、纯本地可完成 |
| 建库耗时目标 | 分钟级（local 后端含首次模型下载） | BGE 模型约 95MB（`HF_ENDPOINT` 镜像可选）；cloud 后端取决于 API 延迟 |
| Embedding 批处理 | cloud batch=10 / local batch=16 | 控制内存与请求数 |
| 检索深度 | 两路候选 20 → RRF 融合 → Top-5 | RRF 必须大于展示数，否则融合退化 |
| 确定性 | node_id / index_id / 语料指纹均由内容哈希派生 | 同语料同配置 → 同 index_id，幂等重建 |
| 一致性 | manifest 全量校验（embed/chunk/corpus/bm25） | 配置或语料变化 → 显式拒绝（RAG-E02/E04），不静默降级 |
| 可观测 | stdout 与 `output/*.md` 逐字同源（stderr 仅诊断）；每步输出节点数/来源/分数语义/断言结果 | 截图与复核同源 |
| 兼容性 | Windows + PowerShell 5；Python 3.12；纯 wheel 依赖；stdout UTF-8 重配置 | 避开本地编译与编码乱码 |
| 健壮性 | 建库失败不留半成品；旧索引与指针在失败前后不变 | 临时目录 + 原子指针 |

---

## 11. 安全要求

1. **密钥管理**：全部密钥经 `rag/.env` 注入（`python-dotenv`），`rag/.gitignore` 忽略 `.env`；不使用 `pi-ltm/.env`，两个作业配置完全独立；
2. **密钥不回显**：错误信息、`safe_summary`、报告文件只出现变量名（如 `DEEPSEEK_API_KEY`），任何路径不打印 key 值；
3. **数据隔离**：检索语料只读 `rag/data/` 副本；第三步只改副本文件，原始 `login_demo` 资料不被修改；`data/` 与 `storage/`、`output/` 分离；
4. **路径约束**：脚本固定读写工程内目录（`data/storage/output`），不接受外部路径参数，无路径穿越面；
5. **网络面**：外发仅 Embedding 与 LLM API 调用；本地索引加载不执行任何来自资料文件的代码（`.ts` 仅当纯文本读取）；
6. **输出安全**：报告文件不写入密钥、不写入完整 API 响应载荷。

---

## 12. 部署说明

### 12.1 前置条件

- Windows + PowerShell 5；Python 3.12 可用；
- 网络可访问 PyPI；local 后端需可访问 HuggingFace（或镜像 `https://hf-mirror.com`）；
- 已持有 `DEEPSEEK_API_KEY`（必需）；`DASHSCOPE_API_KEY`（仅 cloud 后端，本版默认 local 暂不填）。

### 12.2 安装与配置

```powershell
cd c:\projet\homework\ch4\rag
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local-embed.txt   # local 后端（含 torch + sentence-transformers）
Copy-Item .env.example .env                    # 填入 DEEPSEEK_API_KEY，保持 RAG_EMBED_BACKEND=local
```

### 12.3 资料准备（一次性）

1. 为 `login_demo/artifacts/login-flow.md` 末尾追加 §6「会话有效期」（初始值 **15 分钟**）；
2. 复制资料到副本：

```powershell
python -c "import pathlib,shutil; s=pathlib.Path(r'..\login_demo'); d=pathlib.Path('data'); d.mkdir(parents=True, exist_ok=True); shutil.copy2(s/'README.md', d/'README_login_demo.md'); shutil.copytree(s/'artifacts', d/'artifacts', dirs_exist_ok=True); shutil.copytree(s/'src', d/'src', dirs_exist_ok=True)"
```

### 12.4 运行（三步 + 三张截图）

```powershell
python scripts\step1_build_index.py      # 建库 → output/step1_build_index.md
python scripts\step2_compare_query.py    # 三路对比 + 混合回答 → output/step2_compare_query.md
python scripts\step3_update_verify.py    # 更新 + 重建 + 断言 → output/step3_update_verify.md
python scripts\step3_update_verify.py --restore  # 恢复 15 分钟基线 → output/step3_restore.md
```

截图直接截屏幕；复核以 `output/*.md` 为准（stdout 与文件逐字一致，stderr 诊断不写入报告）。

### 12.5 环境变量清单（`.env.example`）

```ini
RAG_EMBED_BACKEND=local                       # cloud | local（本版默认 local）
LOCAL_EMBED_MODEL=BAAI/bge-small-zh-v1.5      # local 后端模型
LOCAL_EMBED_DIM=512
HF_ENDPOINT=https://hf-mirror.com             # 模型下载镜像（可选）
DASHSCOPE_API_KEY=                            # cloud 后端时必填
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
RAG_EMBED_MODEL=text-embedding-v3             # cloud 后端模型
RAG_EMBED_DIM=1024
DEEPSEEK_API_KEY=                             # 必需（主模型）
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
RAG_LLM_MODEL=deepseek-chat
QWEN_FALLBACK_API_KEY=                        # 可选（仅故障时）
QWEN_FALLBACK_BASE_URL=https://portal.qwen.ai/v1
QWEN_FALLBACK_MODEL=qwen3-coder-plus
RAG_CHUNK_SIZE=384
RAG_CHUNK_OVERLAP=76
RAG_TOP_K=5
RAG_CANDIDATE_K=20
```

### 12.6 切换 cloud 后端（后续）

1. 填写 `DASHSCOPE_API_KEY`，改 `RAG_EMBED_BACKEND=cloud`；
2. **必须重新运行 step1 重建索引**（维度 512 → 1024，旧索引会被 `RAG-E02` 显式拒绝，不会静默出错）。

---

## 13. 测试与验收决策（Testing Decisions）

### 13.1 测试接缝（Seams）

- **最高接缝**：三个脚本入口（`scripts/step*.py`）——验收与截图均在此层进行，不深入模块内部实现；
- **第二接缝**：`indexer.load_index` 的兼容性/陈旧性校验——用于验证"配置变化被拒绝"与"资料变更被拒绝"；
- **第三接缝**：`ChineseBM25Retriever.from_persist_dir` 的分词标记校验——用于验证契约 c（RAG-E02）。

### 13.2 自检与断言（内建，不需额外测试框架）

| 位置 | 自检项 |
|---|---|
| step1 | 双路节点数 == 语料节点数；vector id 集合 == bm25 id 集合（同一批节点）；`loaded.bm25` 对默认问题 Top-1 可命中；不一致 → `RAG-E05` |
| step2 | 三路各 Top-5 非空且带来源；hybrid Top-5 的 `node_id` 唯一；默认问题双路命中 > 0；引用编号解析（越界检测）；`citation_ok` 结论输出 |
| step3 | `[0]` §6 前置检查与基线快照；A1（软提示）旧索引召回旧值；A2/A3 索引层面 §6 旧值退出/新值进入；A4/A5 检索层面 §6；A6 节点数一致；A7/A8 回答与引用层面；`--restore` 校验 R1–R3 |

### 13.3 行为契约（外部行为，非实现细节）

1. 同语料 + 同配置两次建库 → 同一 `index_id`，第二次幂等复用；
2. 手改 `bm25/retriever.json` 的 tokenizer 标记 → 查询立即 `RAG-E02`/exit 4，不降级；
3. 卸载 jieba 后运行 → `RAG-E01`/exit 2，绝不回退默认分词；
4. 语料改动后直接查询 → `RAG-E04`（陈旧），`--allow-stale` 显式放行；
5. 删除 `current.json` 后查询 → `RAG-E04`；`--auto-build` 显式放行并重建；
6. step3 重跑（副本已是 20 分钟）→ 幂等：跳过 A1 基线对照，其余断言仍通过；
7. step3 `--restore` → 读取 15 分钟基线快照，恢复副本并重建双路索引，R1–R3 通过，产物 `output/step3_restore.md`；
8. 报告中不出现任何密钥值。

### 13.4 验收口径（对应 q4-2 提交物）

- 截图 1（step1）：节点数量 + 一条带来源分块样例（source/章节/行号/hash）；
- 截图 2（step2）：三路 Top-5 各自来源 + RRF 混合回答 + 引用清单；
- 截图 3（step3）：改写前后对比 + 重建 + A1–A8 断言 + 更新后回答引用新资料；
- 附加证据（可选）：`--restore` → `output/step3_restore.md`，证明 15 分钟基线可恢复。

---

## 14. 扩展性考虑

| 方向 | 预留设计 | 演进路径 |
|---|---|---|
| Embedding 后端切换 | `RAG_EMBED_BACKEND` 开关 + manifest 维度校验 | 新增后端仅需扩展 `providers.get_embed_model` 与 `_check_compat` |
| LLM 多模型 | `LLMConfig` 主备链 | 可扩展为 N 模型顺序故障转移 |
| 检索升级 | `Row.hits` 多路命中标记已就绪 | 可加 Rerank（LLM/Cross-Encoder）在 RRF 之后、生成之前插入 |
| 元数据过滤 | 节点已带 `source/section/file_sha256` | 可加 `MetadataFilters`（两路同范围） |
| 评估 | 三步入口与 `--question` 参数 | 可加固定问题集跑召回率/命中率（对应课程 §4.4 评估口径） |
| 检索-only 模式 | `answering` 与检索解耦 | 可加 `--no-llm` 输出仅检索结果与引用（对应课程 §4.6 `--json --no-llm`） |
| 关键词引擎替换 | BM25 封装在 `retrieval.py` 单点 | 可替换为 Elasticsearch 承担关键词一路，RRF 融合接口不变 |
| 多项目 | 节点元数据可加 `project` 字段 + manifest 分项目 | 对应课程"项目维度用于过滤/隔离" |
| Agent 接入 | 三步脚本为纯函数式流水线 | 按课程 §4.6 包装为只读工具 `rag_query`（回答 + 编号来源追溯），先独立验证再接 |

---

## 15. 与课程文档对照结论

对照 `docs/第四章_知识点总结与逻辑框架.md` §4.4/§4.6 的逐项分析（详见评审记录）：

- **匹配项（16）**：分块→Embedding→双路索引→RRF 主干、Embedding 一致性硬校验、索引建/开分离、中文 BM25 一致分词、RRF 分数语义标注、引用三层第一层、`ingest/query` 职责分离、BGE 512 维本地向量等；
- **部分匹配（5）**：缺清洗独立环节、缺问题处理与重排、元数据过滤缺 filter、生成实际输入未落盘、引用校验仅第一层通用；
- **差异项（6，有意取舍）**：无 project 维度、删除=版本切换、bm25s 而非 ES、关闭查询改写（三路必须同问题）、展示层截断不进 prompt、A7 答案关键词断言（作业口径）；
- **超文档项（14，保留）**：版本化索引 + 原子指针、manifest 自描述、确定性 node_id、BM25 分词标记校验、统一错误码、三道防线、Tee 同源输出等；
- **文档模糊项（12，按用户要求不调整方案）**：如"RRF k 值未给来源""召回率 gold 口径未定义"等，仅记录。

---

## 16. 已知限制（后续演进清单，本版不实现）

| ID | 限制 | 课程依据 | 影响 |
|---|---|---|---|
| D1 | 引用校验仅第一层（编号合法）通用实现；第二层（实体核对）、第三层（结论支持）仅 step3 固定问题断言覆盖 | §4.4 Citation 三层检查 | `citation_ok` 语义为"编号合法"，不宣称"结论被证据支持" |
| D2 | 校验失败后的发布行为未定义（仍输出回答、exit 0） | §4.4 校验失败后的发布 | 后续可加"标缺口/修复后重检/拒答"策略 |
| D3 | 生成实际输入（完整 prompt）未落盘 | §4.4 重排实际输入 | `AnswerResult.prompt` 已构造，后续可写 `output/` |
| D4 | A7 用答案关键词验收 | §4.6 验收看证据 | 属 q4-2 作业口径；结论以 A2/A4/A8 为主 |
| D5–D14 | 无 Rerank、无评估集与指标、无 Metadata Filter、无 token/延迟统计、分块未按标题/AST、BM25 未保护路径标识符、无资料-源码冲突检测、无分块覆盖率校验、反例覆盖不足、清洗环节缺失、降级口径未标注、无 `--no-llm` 模式、step1 无重建确认 | §4.4/§4.6 各环节 | 多为作业范围外或低成本后续项 |

---

## 17. 范围外（Out of Scope）

1. Agent 集成与工具运行时（RBAC/审批/审计）——第二题独立验证 RAG 后端；
2. Rerank（LLM/Cross-Encoder）、查询改写/分解/澄清、HyDE；
3. 评估集、gold 标注、召回率/命中率/V0–V3 对比实验；
4. Elasticsearch、ChromaDB/FAISS/Milvus 等外置存储；
5. 多项目隔离、权限模型、资料-源码冲突检测；
6. `login_demo` 源码修改（仅补资料文档 §6）。

---

## 18. 附注（Further Notes）

1. **当前实施状态**：本规格基于 grill-me 三轮评审与课程文档对照分析定稿；`rag/` 下代码已落盘，`login-flow.md` 的 §6 已应用；step1/step2/step3 已运行，step3 支持 `storage/.baseline/login-flow.md` 基线快照与 `--restore`；
2. **官方 API 核验结论**：`llama-index-retrievers-bm25 0.5.2` 的 `from_defaults(tokenizer=...)` 会丢弃 tokenizer 参数（仅 deprecation warning），因此中文 BM25 采用 `ChineseBM25Retriever` 子类（§6.5 四条契约）；`QueryFusionRetriever` 在 `num_queries=1` 时不调用 LLM，但构造时仍需显式传入 LLM 以规避 `Settings.llm` 隐式回退；
3. **资料语义**：原始资料 §6 保持 15 分钟；第三步仅在 `rag/data/` 副本中改为 20 分钟；运行前把 15 分钟快照保存到 `storage/.baseline/login-flow.md`，`--restore` 可恢复；`data/` 副本是唯一检索语料；
4. **评估诚实性**：本版报告只陈述"三路对比结果与断言结果"，不做"混合检索优于单路"的提升宣称（对应课程 §4.4"组件增加不是提升证据"）；
5. **文档问题记录**：课程文档中 12 处模糊/不准确表述（RRF k 值来源、gold 口径、V0–V3 定义等）按用户要求仅记录、不据此调整本方案。
