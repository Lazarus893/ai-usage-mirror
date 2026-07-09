---
name: ai-usage-mirror
description: >-
  照一面"AI 使用行为的镜子":读取本地 Claude Code + Codex 的真实交互记录,归一进 SQLite,
  产出你的 AI 使用习惯画像、常用代码/任务、协作摩擦点、重复求助。当用户说"分析我的 AI 使用习惯 /
  AI 使用报告 / 我常用什么任务 / 我常让 AI 干什么 / 复盘我怎么用 AI / my AI usage habits /
  ai usage mirror / 我的 AI 使用画像"时触发。本地只读、零上传。
---

# ai-usage-mirror

给"我"(Claude)一面镜子:回顾用户跟本地各 AI 助手的真实交互,总结使用习惯与常用任务/代码。
架构与实现细节见同目录 `ARCHITECTURE.md`;所有确定性活由 `scripts/mirror.py` 完成,语义判断由本 skill(装载它的我)完成。

## 何时用
用户想**回顾/复盘自己怎么用 AI**、想知道**常做哪些任务、常写哪些代码、哪类需求反复返工**时。
不是实时编排(那是别的工具),是**回顾性个人画像**。

## 数据边界(先说清,别过度承诺)
- v1 只覆盖**留下本地结构化记录的 CLI/编辑器类助手**:Claude Code(`~/.claude/projects`)+ Codex(`~/.codex/sessions`)。
- ChatGPT/Claude 桌面版对话在服务端或 IndexedDB,**本地拿不到**,不在范围。
- 全程**只读源、脱敏后入库、绝不上传**。

## 执行流程(我按此跑)

工作目录 = 本 skill 目录。所有命令 `--json` 时 **stdout 只出数据、stderr 出进度**;按语义退出码分支。

1. **预检**:`python3 scripts/mirror.py triage`
   - exit 0 → 就绪,读其 `next_command`;exit 3(空库)→ 先 `ingest`;exit 6(版本不符)→ `ingest --full`。
2. **增量入库**:`python3 scripts/mirror.py ingest`(通常 <0.2s;首次约 2s)。
3. **取聚合**:`python3 scripts/mirror.py digest --json` → 读 stdout 的 digest(≤30KB,**只有它进我的 context,原始 transcript 永不进**)。
4. **取任务簇**:`python3 scripts/mirror.py cluster --json`(有 `fastembed` 时可加 `--embeddings` 提质;没有会自动降级 TF-IDF)。
5. **按需取证**(可选):对某主题想要原话佐证时 `python3 scripts/mirror.py pack "<主题词>" --max-tokens 3000`。
6. **生成报告页并唤起**:`python3 scripts/mirror.py report` → 把真实数据注入 `report_template.html`,写出 `.state/report.html`(编辑部式审计手记,单文件可双击),**默认自动在浏览器打开**供审阅(`--no-open` 可抑制)。
7. 同时在对话里基于 digest + clusters 写**五段报告**(见下)作为速览,收尾询问是否落盘到 `memory/`。

## 读数纪律(关键,别读错)
digest 是**分层**的,解读时必须区分:
- **你的直接投入**(会话数 / 作息 `temporal` / 摩擦 `friction_candidates` / `task_prompts`)= 仅 `real` 会话(你亲手开的顶层对话)。
- **发生的工作量**(`tools_used` / `commands_top` / `edited_filetypes` / `models` / `output_tokens_est` / `projects`)= `real + meta`,**含你委派给子 agent 的活**(sidechain)。所以"会话数少但 token/工具量大"是正常的——你重度委派。
- `meta.meta_sessions_skipped`(子 agent/自动会话)、`artifact_sessions_skipped`(TUI `/status` 碎片)是**被正确排除**的,提一句透明度即可。
- **token 口径**:含 reasoning;Codex 的 token 仅近期会话有记录(覆盖不全,别当精确值);模型按会话主导计,非逐轮。

## 五段报告结构
① **使用习惯画像** — 作息(`by_hour`/`by_weekday` 峰谷)、模型偏好(是否只用顶配、是否降档)、工具/委派强度、项目分布(`projects`)、token 量级。
② **常用代码 & 任务** — `commands_top`/`edited_filetypes` 讲"你/AI 常跑什么、常改什么类型文件";`cluster` 的 size≥2 簇由我**语义命名成任务原型**(如"单文件 HTML 原型""Coop 评测语料生成""openclaw 运维")。
③ **协作摩擦** — 从 `friction_candidates` 归纳返工主题(如"审美打磨""说人话/去黑话"),给**可操作建议**(如把 design-skill 调用 + 禁术语写进初始 prompt 以省返工回合)。
④ **重复求助** — 反复出现的簇 → 建议**沉淀成 `memory/` 或 skill**(呼应用户的 memory/experience-library 体系)。
⑤ **反思洞察** — 反直觉的点、效率黑洞、值得改的习惯。诚实,不迎合;必要时点名残留噪声(个别簇可能是短语误合)。

## 交付原则
- 默认 **on-demand**(本 skill 唤起),**不装自动 hook**。
- 报告用中文、术语精准;先给判断再给证据。
- 可选:想让"按需报告秒出",把 `ingest` 挂 launchd 每日预热(仅预热库、不打扰),见 `ARCHITECTURE.md §10`——默认不装。
