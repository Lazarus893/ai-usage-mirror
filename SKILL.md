---
name: ai-usage-mirror
description: >-
  照一面"AI 使用行为的镜子":读取本地 Claude Code + Codex 的真实交互记录,归一进 SQLite,
  产出你的 AI 使用习惯画像、常用代码/任务、协作摩擦点、重复求助;还能蒸馏你的**编码习惯**
  (技术栈指纹 / 需求表达风格 / 验证纪律 / 反复摩擦)成一份两层 markdown。当用户说"分析我的 AI 使用习惯 /
  AI 使用报告 / 我常用什么任务 / 我常让 AI 干什么 / 复盘我怎么用 AI / my AI usage habits /
  ai usage mirror / 我的 AI 使用画像 / 沉淀我的编码习惯 / 我的编码指纹 / 我的技术栈偏好 /
  coding profile"时触发。本地只读、零上传。
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

## 编码习惯沉淀(coding-profile 流程)
当用户想**沉淀/固化编码习惯**、要**技术栈指纹 / 编码画像 / memory-ready 规则**时,走这条(与五段报告正交,可单独跑):

1. `ingest`(增量,同上;若 exit 6 则 `ingest --full` 做 schema v2 迁移,约 2s)。
2. **取 profile 摘要**:`python3 scripts/mirror.py profile --json` → stdout 出 `{profile, clusters, span}`(只有它进 context)。四组维度:
   - `prompting_style` — 指令粒度/长度分布/中英比/带验收率/先讨论倾向(仅 real,你的直接投入)。
   - `stack_fingerprint` — `packages_installed`(AI 实际装的,含子 agent)、`libs_mentioned`(你 prompt 点名的)、`package_managers`、`languages`。
   - `verification_habits` — test/git/typecheck/build 分桶 + commit 数 + test:edit 比。
   - `friction_themes` — 摩擦引用粗聚成主题桶(审美/去黑话/禁 mock/对齐/信息过载/返工/其他)。
3. **我合成两层 md 并落盘**(这步是判断活,归我):
   - **Tier A 画像**(描述型):基于四组维度写"你作为 coder 的指纹",技术栈簇由我语义归并(如把 `next`/`next.js` 合一)。
   - **Tier B 候选规则**(memory-ready):把 `friction_themes` 里 n≥2 的主题**改写成精准、可执行的规则**(建议型不是质问型,呼应你 xlsx 的"缺口 Tips"姿态),每条给证据引用。
   - 直接把最终 md 用 Write 写到 `.state/coding-profile.md`(或用户指定路径)。若不需要我增强,`mirror profile`(不带 `--json`)会渲染一份确定性兜底 md。
4. **收尾**:把 Tier B 的候选规则逐条念给用户,**问是否并入 `memory/` 或 `CLAUDE.md`**——不自动写这两处(尊重用户视 CLAUDE.md 洁净为关键)。

> 读数纪律同上:`prompting_style`/`friction_themes` 是 real(你的直接表达);`stack_fingerprint`/`verification_habits` 含 meta(子 agent 替你干的活)。`cmd_key` 只留前 2 token,`npm run *` 粗归 build 桶——直接跑的 pytest/vitest/playwright/tsc/git commit 才精确。

## 导出「元宝输入法(Handy)」上下文(input-profile 桥)
当用户想把 AI 使用史**喂进元宝输入法/Handy 做本地上下文**、要"导出输入法画像 / input profile"时,走这条(与报告正交):

1. `ingest`(增量,同上;确保 `.state/digest.json` 是新的:`python3 scripts/mirror.py digest --json > .state/digest.json`)。
2. **导出工件**:`python3 scripts/export_input_profile.py` → 写 `.state/input_profile.json`(schema `yuanbao-input-profile/v2`)。脚本干确定性蒸馏、只读、不联网,自动读 `digest.json` + `mirror.db`;缺库用 `--no-db` 降级。产物拆成 Handy 两条原生通道:
   - `terms` —— 领域高频专名(项目名 / CamelCase 库 / 缩写 / 高频中文)→ Handy **词典 hotword**(改 ASR/refinement 对专名的识别)。这是「上下文/字面命中」通道,宁缺毋滥。
   - `facts` —— 从真实使用史归纳的**结构化记忆**(topic + fact):`技术栈`(合并 libs_mentioned+packages_installed,分组:前端/编辑器/数据后端/测试/AI-Agent/桌面基建/数据处理)、`语言`(主力扩展名)、`项目`(常做仓库)、`协作风格`(中文/简短/带验收/先讨论)→ Handy **memory_facts**(带各自 topic)。给 refinement 定调、给 VP c-mode「项目 Prompt」policy 一份**有数据支撑**的工程约束先验(替代原来硬编码 demo seed)。
   - `topics` —— v1 legacy 项目占比句,仅为老 Handy 兼容保留;新 Handy 有 `facts` 时忽略。
3. **Handy 侧落库**(两条路并存,都走 `commands/usage_profile.rs`,幂等,terms→dictionary、facts→memory_facts,按文本去重可反复导):
   - **零点击(推荐)**:Handy 启动时 `auto_import_if_newer` 自动检测默认工件,只在工件 mtime **比上次导入更新**时才静默重导(水位记在 app 数据目录 `usage_profile_import.json`)。导入过就不再和用户手动增删较劲;想强制重导 = 重跑本步 export(mtime 自然前进)。失败绝不拦启动。
   - **手动**:设置页「上下文」tab 顶部「导入 AI 使用画像」按钮 → `import_usage_profile`,立刻导入 + 回显统计 + 刷新词典/记忆列表。
   > 于是「跑 skill 生成工件 → 下次启动 Handy 自动吃」成零点击闭环;按钮留作"我现在就要导 + 看结果"。
> 采信阈值分来源:AI 实际**安装**的包 ≥1 次即采信(装了就用),只在 prompt **提到**的库要 ≥2 次。技术栈 fact 优先(对 VP 最有用),协作风格垫底;总数 ≤14 条留余量给用户自加记忆。

## 交付原则
- 默认 **on-demand**(本 skill 唤起),**不装自动 hook**。
- 报告用中文、术语精准;先给判断再给证据。
- 可选:想让"按需报告秒出",把 `ingest` 挂 launchd 每日预热(仅预热库、不打扰),见 `ARCHITECTURE.md §10`——默认不装。
