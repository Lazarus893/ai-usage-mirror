# ai-usage-mirror — 架构定稿 v1

> 读取本地主流 AI 助手的交互记录 → 归一存储 → 聚合出使用习惯画像、常用代码/任务、协作摩擦与重复求助。
> **哲学**:脚本干确定性(定位/解析/归一/统计/检索),LLM 干判断(语义命名/聚类精判/洞察)。原始 transcript 永不进 LLM context。
> **来源**:v1 = Claude Code + Codex(JSONL);v2 = Cursor(vscdb)/Gemini/git log/recent files。
> **借鉴**:CASS(SQLite 权威源 + 归一 schema + robot-mode + fail-open),Suvadu(prompt→action 溯源 + executor),Engram(夜间预算 + "capture what you did")。

---

## 1. 四层管线

```
Discover → Extract(归一) → Store(SQLite 权威源) → Aggregate(digest) → Report(LLM)
                                    └→ Index(FTS5 + 可选 embedding)→ pack/search/cluster
```
- **Store = 唯一真相源**;digest.json / FTS5 索引 / 向量都是**派生资产**,随时从 SQLite 重建(fail-open:派生资产缺失/损坏一律当"重建问题",不逼人手修)。
- **增量**:按文件 (mtime,size,hash) 跳过未变会话;只重解析变更文件。全量 1806 文件当前 1.6s,增量应 <0.2s。

## 2. 目录结构

```
~/.claude/skills/ai-usage-mirror/
  SKILL.md              # LLM 面向:触发词 + report 流程
  ARCHITECTURE.md       # 本文件
  scripts/
    mirror.py           # CLI 入口(argparse 分发)
    store.py            # SQLite schema + 增量 ingest
    sources/
      __init__.py       # SOURCE 注册表 [(name, glob, parser)]
      claude_code.py    # parse_claude()
      codex.py          # parse_codex()
    aggregate.py        # digest(SQL 聚合)
    profile.py          # 编码习惯蒸馏:build_profile(4 组维度)+ render_md(两层 md)
    cluster.py          # ②repeat:FTS5 关键词 / 可选 embedding
    redact.py           # 脱敏(persist 前)
    embed.py            # 可选 ONNX MiniLM(lazy import,不装则降级)
    filters.py          # is_real_prompt/is_task_prompt/is_injected/unwrap/_CORRECTION
  .state/
    mirror.db           # SQLite 权威源(唯一写目标)
    digest.json         # 最近一次派生 digest
    models/             # 可选下载的 ONNX 模型(~90MB)
    VERSION             # schema 版本号(mismatch → exit 6)
```

## 3. 数据模型(SQLite DDL — 权威)

```sql
CREATE TABLE session (
  id           TEXT PRIMARY KEY,   -- 'cc:<uuid>' | 'cx:<id>'
  source       TEXT NOT NULL,      -- 'claude-code' | 'codex'
  file_path    TEXT NOT NULL,
  file_mtime   REAL NOT NULL,      -- ┐ 增量键
  file_size    INTEGER NOT NULL,   -- ┘
  content_hash TEXT,               -- sha1(file),二次确认
  project      TEXT,               -- resolve_project():项目根 或 '<none>'
  cwd_raw      TEXT,
  model        TEXT,               -- 末次/主导模型
  originator   TEXT,               -- codex: codex_cli_rs / Codex Desktop / codex_exec
  started_at   TEXT, ended_at TEXT,
  n_user       INTEGER, n_asst INTEGER,
  out_tokens   INTEGER,            -- output(+reasoning) 估算
  kind         TEXT NOT NULL,      -- 'real' | 'meta' | 'artifact'  ← 见 §4.3
  ingested_at  TEXT
);
CREATE TABLE message (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,    -- 会话内顺序
  ts          TEXT,
  role        TEXT NOT NULL,       -- 'user' | 'assistant'
  text        TEXT,                -- 脱敏后。user 全存;assistant 默认不存正文(见 §4.4)
  is_task     INTEGER DEFAULT 0,   -- is_task_prompt 通过
  is_friction INTEGER DEFAULT 0,   -- 纠正信号命中
  sig         TEXT                 -- 关键词签名(降级聚类用)
);
CREATE TABLE tool_call (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  after_seq   INTEGER,             -- 归属的 assistant 回合 → prompt→action 溯源(Suvadu)
  tool        TEXT NOT NULL,       -- 'Bash'|'Edit'|'codex:exec_command'|...
  cmd_key     TEXT,                -- command_key():命令头(去 cd 导航)
  file_ext    TEXT,                -- Edit/Write 文件扩展名
  workdir     TEXT,                -- 项目兜底来源
  pkgs        TEXT                 -- v2: install_pkgs() 抽的包名(逗号连),供技术栈指纹;无则 NULL
);
CREATE TABLE prompt_vec (          -- 派生:可选 embedding
  message_id  INTEGER PRIMARY KEY REFERENCES message(id) ON DELETE CASCADE,
  vec         BLOB                 -- float32 packed(MiniLM 384d)
);
CREATE VIRTUAL TABLE message_fts USING fts5(text, content='message', content_rowid='id');
CREATE TABLE ingest_state (        -- 增量
  file_path TEXT PRIMARY KEY, file_mtime REAL, file_size INTEGER,
  content_hash TEXT, session_id TEXT
);
CREATE INDEX idx_msg_session ON message(session_id, seq);
CREATE INDEX idx_tc_session  ON tool_call(session_id);
CREATE INDEX idx_sess_kind   ON session(kind, source);
```

## 4. Extract 层(归一规则 — 已在原型验证,不可回退)

### 4.1 来源 schema(实测)
- **Claude Code** `~/.claude/projects/**/*.jsonl`,每文件一会话:`type=user`(message.content str/含 tool_result 的 list)、`type=assistant`(message.model + usage.output_tokens + content 内 `tool_use{name,input}`)。
- **Codex** `~/.codex/sessions/**/*.jsonl`:`session_meta{id,cwd,originator}`、`turn_context{model,cwd}`、`event_msg/user_message{message}`、`event_msg/agent_message`、`event_msg/token_count.info.total_token_usage`、`response_item/function_call{name,arguments:{cmd,workdir}}`。

### 4.2 归一到 message/tool_call
- user 文本:`unwrap()` 剥 "My request for Codex:" 类信封 → `is_injected()` 挡审批/队友/系统前言 → `is_real_prompt()`(挡 `/斜杠命令`、`<`、meta 前缀)。过者入 message(role=user)。
- assistant:model 记 session.model;`usage.output_tokens`(Claude)累加;`tool_use`/`function_call` 入 tool_call,`after_seq` = 当前 assistant 回合序号。
- **token(修正过的坑)**:Codex `total_token_usage` 是**会话累计** → 取 `max(output_tokens + reasoning_output_tokens)`(不是逐条求和,那会虚高 60×);Claude 按每条 assistant `output_tokens` 累加。仅近期 Codex 会话有 token_count → 覆盖不全,digest 标注。

### 4.3 会话分类 kind(关键防污染 — 已验证)
- `n_user==0` 且有 assistant → **`meta`**(审批 review / 自动子会话),不计入使用统计。
- 有 user 但**无一条 `is_task_prompt`** → **`artifact`**(纯 TUI `/status` 碎片;`codex_cli_rs` 曾伪造 1010 个)。
- 其余 → **`real`**。统计/画像**只用 `real`**;meta/artifact 计数透明上报,不静默丢。

### 4.4 存储边界(控 DB 体积)
- user message 正文**全存**(脱敏后,聚类/摩擦/pack 都要)。
- assistant 正文**默认不存**(28M tokens 会撑爆库),只留 model/token/tool_call。`--full-text` 时才存 assistant 正文进 FTS5(供全文 search/pack)。

### 4.5 关键过滤器(filters.py,原型实测参数)
- `is_task_prompt`:len≥12 且(含 CJK 或 **去重后 ≥3 个不同词**)。"去重"是防 `status/status /status` 这类重复 token 混过。
- `_CORRECTION`(①摩擦召回):`不对|还是不|太丑|术语太多|信息过载|没对齐|不够|不符合|没有按照|重新|actually|wrong|revert|...`;命中须在**首 120 字**且整条 <600 字(挡长注入)。
- `project_root(path)`:redact 后按 `Projects/Downloads/Desktop` 锚点取项目根;cwd 无效(`/`,`~`)时用 tool_call.workdir / `cd` 目标 / 编辑文件路径**兜底**。

## 5. Aggregate 层 → digest.json(SQL 聚合,派生)

固定形状(≤35KB,当前已达):
```jsonc
{ "meta": {sources, sessions, files_scanned, meta_sessions_skipped, artifact_sessions_skipped, tokens_note, span},
  "temporal": {by_hour[24], by_weekday[7]},
  "projects": [{cwd, sessions, share}],           // 仅 kind=real
  "tools_used": {tool: n}, "commands_top": [{cmd, n}],   // tool_call 聚合
  "edited_filetypes": {ext: n}, "models": {model: n},
  "output_tokens_est": {source: n},
  "friction_candidates": [{source, session, quote}],     // ① is_friction 去重
  "task_prompts": [{prompt, n, source}],                 // ② 去重任务 prompt,喂给 §7 聚类
  "per_source": {source: real_sessions} }
```

## 5.5 编码习惯蒸馏 → coding-profile(profile.py,派生)

借「类目树」方法论(MECE 一级维度 + 横切参数不入主维度)从记录蒸馏**编码习惯**——digest 答"怎么用 AI",profile 答"我是个怎样的 coder"。`build_profile(db)` 出 4 组维度(工作流/项目/任务原型已在 digest+cluster,不重复):

```jsonc
{ "prompting_style":    {n_prompts, median_chars, length_mix, cjk_share, acceptance_rate, discuss_first_rate},  // real
  "stack_fingerprint":  {packages_installed[], libs_mentioned[], package_managers, languages},                  // real+meta
  "verification_habits":{buckets{test,git,typecheck,build}, commits, edits, test_to_edit_ratio},                // real+meta
  "friction_themes":    [{theme, n, quotes[]}] }                                                                 // real
```
- **确定性抽取**在 `filters.py`:`install_pkgs`(留 install 命令的包名,command_key 会丢)、`libs_in_text`(LIB_LEXICON 词表命中)、`mgr_head`(管理器白名单,挡 `mkdir/ssh` 复合命令噪声)、`verif_bucket`、`has_acceptance/wants_discussion`。
- **两态输出**(仿 Baseline/C-mode):`render_md` 出确定性两层 md 兜底(Tier A 画像 + Tier B 候选规则种子);`--json` 交 LLM 合成增强版并 Write 落盘(语义归并栈、把 n≥2 摩擦主题改写成 memory-ready 规则)。
- **口径**同 digest 分层:`prompting_style`/`friction_themes` 仅 real;`stack_fingerprint`/`verification_habits` 含 meta(子 agent 替你装的库/跑的测试)。
- **已知粗粒度**:`cmd_key` 只留前 2 token → `npm run test/build/dev` 都塌成 `npm run`(粗归 build);直接跑的 pytest/vitest/playwright/tsc/git commit 精确。

## 6. Index & 检索(FTS5 必备 / embedding 可选)

- **词法(必备,零依赖)**:sqlite3 内置 **FTS5** over `message.text`,支撑 `search` / `pack` 的秒级前缀匹配(`snake_case`/代码符号)。
- **语义(可选,opt-in)**:`embed.py` lazy import `onnxruntime`+MiniLM(~90MB),对 `is_task=1` 的 prompt 生成 384d 向量入 `prompt_vec`。**不装则整条降级到词法**(CASS fail-open),默认不装、不联网。

## 7. ②repeat 聚类(cluster.py — 救活判死的那层)

- **默认(词法)**:按 `sig` 去重 + FTS5 共现,给 LLM 一批候选(弱,但零依赖)。
- **`--embeddings`(推荐)**:对 task-prompt 向量做**贪心凝聚**(cosine ≥0.82 归一簇,dep-light 不引 HDBSCAN),每簇出 {代表 prompt, n, 成员会话}。→ 真正的"常用任务类型"。
- 输出交 Report 层由 LLM 命名任务原型("单文件 HTML 原型 + Playwright 点测""Coop 评测语料生成"…)。

## 8. CLI / robot-mode 契约(借 CASS)

| 子命令 | 作用 | 输出 |
|---|---|---|
| `mirror ingest [--full]` | 解析 → SQLite(默认增量) | 进度→stderr |
| `mirror digest [--json]` | 从 SQLite 重建 digest | JSON→stdout |
| `mirror cluster [--embeddings] --json` | ②任务聚类 | JSON→stdout |
| `mirror profile [--json] [--out PATH]` | 蒸馏编码习惯:`--json`→摘要给 LLM;默认渲染两层 md | JSON→stdout / md→文件 |
| `mirror pack "<q>" --max-tokens N --json` | **token 预算内**带引用摘录(agent 交接) | JSON→stdout |
| `mirror search "<q>" [--json]` | FTS5(+可选语义) | JSON→stdout |
| `mirror triage --json` | 首调预检:库状态/计数/`next_command` | JSON→stdout |
| `mirror doctor --json` | 健康:完整性/覆盖率/safe-to-gc | JSON→stdout |
| `mirror capabilities --json` | 自描述 API | JSON→stdout |

**纪律**:`--json` 时 **stdout 只出可解析数据,stderr 出进度/诊断/teaching note**。
**语义退出码**:`0` ok / `1` 健康失败 / `3` 空库或缺索引(可重试→先 ingest)/ `5` 损坏(quarantine 不删)/ `6` schema 版本不符。
**契约稳定**:digest/pack 的 JSON 用 **golden-file 回归测试**锁字段,改字段即挂测试。

## 9. 隐私 & 安全(硬约束)

- **只读源**:绝不写 `~/.claude/projects`、`~/.codex`;唯一写目标 `.state/`。
- **脱敏在 persist 前**:`sk-*`/bearer/email/`api_key=…`/`$HOME→~`,存进 SQLite 的就已脱敏。
- **本地优先、零遥测**;唯一可选联网 = embedding 模型下载(显式 `--embeddings` 才触发)。

## 10. 交付(尊重 on-demand 偏好,不自动 hook)

- **主路**:slash `/ai-usage-mirror`(SKILL.md 触发) → `ingest`(增量)→ `digest`/`cluster` → LLM 出报告。
- **可选新鲜度**:launchd 每日跑一次 `ingest`(仅预热库,**不注入、不打扰**),让按需报告秒出。
- **可选 v2 交付**:MCP server 暴露 `search`/`pack`,任意会话按需反查(比 Engram 的 hook 注入更合偏好)。
- **明确不做**:SessionStart/Stop 自动注入(违背"按需触发"原则)。

## 11. SKILL.md report 流程(LLM 面向)

触发词:`分析我的AI使用习惯 / AI使用报告 / 常用任务 / my AI habits / ai usage mirror`。
步骤:`ingest`(增量)→ `digest --json` + `cluster --embeddings --json` → LLM 读派生结果,产出五段报告:
① 使用习惯画像(作息/模型/工具/项目/token)② 常用代码&任务(命令/文件类型/任务簇)③ 协作摩擦(friction,给可操作建议如"前置 design-skill + 禁黑话")④ 重复求助(该沉淀成 memory/skill 的)⑤ 反思洞察。
必要时 `pack "<主题>"` 取证据摘录。收尾:询问是否把关键洞察写入 `memory/` 或 `experience-library.md`。

## 12. 开发里程碑(进入正式开发)

- **M1 Store 地基**:`store.py`(DDL+增量)+ 把原型 parser 拆进 `sources/`。验收:1806 文件→SQLite,增量重跑 <0.2s,`session.kind` 分类与原型一致(real 128/92)。
- **M2 Aggregate+Index**:`aggregate.py` digest 与当前原型**逐字段对齐**(golden-file)+ FTS5 `search`/`pack`。
- **M3 Cluster**:`cluster.py` 词法默认 + `--embeddings` 语义(embed.py opt-in 降级)。
- **M4 CLI 规范**:robot-mode(triage/doctor/capabilities/pack)+ stdout/stderr/退出码 + golden 测试。
- **M5 交付**:SKILL.md + report 流程;可选 launchd 预热。

## 13. 待定(开工前拍板)

1. **embedding 默认关**(零依赖,`--embeddings` 才开)——推荐,符合 Engram/你的零依赖倾向。
2. **launchd 预热默认关**,提供开关——推荐。
3. **assistant 正文默认不存**(`--full-text` 才存)——推荐,控库体积。
```

- 三条我都倾向"默认保守/opt-in",若无异议即按此开工。
