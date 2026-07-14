"""Coding-habit profile: distill 4 habit dimensions the base digest doesn't capture, then
render a two-tier markdown (Tier A descriptive fingerprint + Tier B memory-ready candidate rules).

Philosophy (same as the rest of the skill): the SCRIPT does deterministic extraction/counting;
the LLM (SKILL.md profile flow) does the semantic naming and rule-authoring on top of `--json`.
render_md() is the deterministic *baseline/fallback* so a file always exists even without the LLM.

Dimensions (see the borrowed category-tree method — MECE first-level, cross-cutting params stay
orthogonal): 1 prompting_style, 2 stack_fingerprint, 4 verification_habits, 5 friction_themes.
(Workflow/delegation, projects and task-archetypes already live in digest.json + cluster.py.)
"""
import collections
import filters as F

WORK = ('real', 'meta')

# Friction theme buckets — coarse keyword routing; the LLM refines the naming afterwards.
_FRICTION_THEMES = [
    ('审美 / UI 打磨', r'丑|难看|审美|不好看|配色|色|样式|太素|视觉|排版|间距|字号|对不齐|层级'),
    ('去黑话 / 说人话', r'黑话|术语|说人话|通俗|太专业|大白话|别用.{0,3}词'),
    ('禁 mock / 要真实', r'mock|假数据|真实数据|写死|hard ?code|硬编码|造数据|占位|placeholder'),
    ('对齐 / 按要求来', r'没对齐|不符合|没有按照|按我说的|不是我要的|没按|跑偏|偏离|自作主张|别自己'),
    ('信息过载 / 精简', r'太多|信息过载|太长|啰嗦|冗余|精简|删掉|去掉.{0,4}多余|太啰'),
    ('返工 / 推倒重来', r'重新|重做|revert|undo|再来|推倒|回退|从头'),
]
import re as _re
_FRICTION_RX = [(name, _re.compile(pat, _re.I)) for name, pat in _FRICTION_THEMES]


def _median(xs):
    if not xs:
        return 0
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def build_profile(db):
    ph = ','.join('?' * len(WORK))

    # ---- 1. prompting_style (real user prompts only = your direct instructing) ----
    texts = [r[0] for r in db.execute(
        "SELECT m.text FROM message m JOIN session s ON m.session_id=s.id "
        "WHERE s.kind='real' AND m.role='user' AND m.text IS NOT NULL")]
    n = len(texts) or 1
    lens = [len(t) for t in texts]
    buckets = collections.Counter()
    for L in lens:
        buckets['terse (<40)' if L < 40 else 'detailed (>400)' if L > 400 else 'normal (40-400)'] += 1
    prompting_style = {
        'n_prompts': len(texts),
        'median_chars': round(_median(lens), 1),
        'length_mix': dict(buckets),
        'cjk_share': round(sum(1 for t in texts if F.CJK.search(t)) / n, 3),
        'acceptance_rate': round(sum(1 for t in texts if F.has_acceptance(t)) / n, 3),
        'discuss_first_rate': round(sum(1 for t in texts if F.wants_discussion(t)) / n, 3),
    }

    # ---- 2. stack_fingerprint (real+meta: includes libs the sub-agents installed for you) ----
    pkg_ct = collections.Counter()
    mgr_ct = collections.Counter()
    for (pkgs, cmd_key) in db.execute(
            f"SELECT pkgs, cmd_key FROM tool_call t JOIN session s ON t.session_id=s.id "
            f"WHERE s.kind IN ({ph}) AND pkgs IS NOT NULL", WORK):
        for p in (pkgs or '').split(','):
            if p:
                pkg_ct[p] += 1
        mgr = F.mgr_head(cmd_key)                     # whitelist -> drops mkdir/ssh/… compound noise
        if mgr:
            mgr_ct[mgr] += 1
    libs_ct = collections.Counter()
    for (t,) in db.execute(
            "SELECT m.text FROM message m JOIN session s ON m.session_id=s.id "
            "WHERE s.kind='real' AND m.role='user' AND m.text IS NOT NULL"):
        for lib in F.libs_in_text(t):
            libs_ct[lib] += 1
    langs = {e: c for e, c in db.execute(
        f"SELECT file_ext, COUNT(*) c FROM tool_call t JOIN session s ON t.session_id=s.id "
        f"WHERE s.kind IN ({ph}) AND file_ext IS NOT NULL GROUP BY file_ext ORDER BY c DESC LIMIT 15", WORK)}
    stack_fingerprint = {
        'packages_installed': [{'name': k, 'n': v} for k, v in pkg_ct.most_common(25)],
        'libs_mentioned': [{'name': k, 'n': v} for k, v in libs_ct.most_common(25)],
        'package_managers': dict(mgr_ct.most_common()),
        'languages': langs,
    }

    # ---- 4. verification_habits (real+meta command heads bucketed; cmd_key is coarse) ----
    vb = collections.Counter()
    commits = 0
    for (cmd_key,) in db.execute(
            f"SELECT cmd_key FROM tool_call t JOIN session s ON t.session_id=s.id "
            f"WHERE s.kind IN ({ph}) AND cmd_key IS NOT NULL", WORK):
        b = F.verif_bucket(cmd_key)
        if b:
            vb[b] += 1
        if cmd_key.lower().startswith('git commit'):
            commits += 1
    n_edits = db.execute(
        f"SELECT COUNT(*) FROM tool_call t JOIN session s ON t.session_id=s.id "
        f"WHERE s.kind IN ({ph}) AND t.tool IN ('Edit','Write','NotebookEdit')", WORK).fetchone()[0]
    verification_habits = {
        'buckets': dict(vb),
        'commits': commits,
        'edits': n_edits,
        'test_to_edit_ratio': round(vb.get('test', 0) / n_edits, 3) if n_edits else None,
    }

    # ---- 5. friction_themes (bucket the correction quotes; LLM renames + turns into rules) ----
    theme_ct = collections.Counter()
    theme_quotes = collections.defaultdict(list)
    for (source, text) in db.execute(
            "SELECT s.source, m.text FROM message m JOIN session s ON m.session_id=s.id "
            "WHERE s.kind='real' AND m.is_friction=1 AND m.text IS NOT NULL ORDER BY m.ts"):
        q = (text or '')[:140]
        hit = next((name for name, rx in _FRICTION_RX if rx.search(text)), '其他')
        theme_ct[hit] += 1
        if len(theme_quotes[hit]) < 3 and q not in theme_quotes[hit]:
            theme_quotes[hit].append(q)
    friction_themes = [{'theme': name, 'n': cnt, 'quotes': theme_quotes[name]}
                       for name, cnt in theme_ct.most_common()]

    return {
        'prompting_style': prompting_style,
        'stack_fingerprint': stack_fingerprint,
        'verification_habits': verification_habits,
        'friction_themes': friction_themes,
    }


# ---------------------------------------------------------------------------
# Junk cluster representatives to skip in the deterministic fallback (the LLM step filters better).
_JUNK_REP = _re.compile(r'^\s*#|OVERRIDE|ssh-ed25519|authorized_keys|Continue from where', _re.I)


def _bar(items, key='name', val='n', top=12):
    if not items:
        return '_(无数据)_\n'
    if isinstance(items, dict):
        items = [{'name': k, 'n': v} for k, v in items.items()]
    lines = [f"- **{it[key]}** ×{it[val]}" for it in items[:top]]
    return '\n'.join(lines) + '\n'


def _inline(d, top=15):
    """Render a {label: count} dict as a compact `a ×5 · b ×3` line (sorted desc)."""
    if not d:
        return '—'
    items = sorted(d.items(), key=lambda kv: -kv[1])[:top]
    return ' · '.join(f"{k} ×{v}" for k, v in items)


def render_md(profile, clusters=None, digest=None, generated=None):
    """Deterministic two-tier markdown. Tier A from stats; Tier B seeds candidate rules from
    friction/repeat signals (marked 候选·待确认). The LLM enriches this on top."""
    ps = profile['prompting_style']
    sf = profile['stack_fingerprint']
    vh = profile['verification_habits']
    ft = profile['friction_themes']
    L = []
    span = (digest or {}).get('meta', {}).get('span') if digest else None
    L.append("# 我的编码习惯画像 (coding-profile)\n")
    L.append(f"> 由 ai-usage-mirror 从本地 Claude Code + Codex 记录蒸馏 · 生成于 {generated or '—'}"
             + (f" · 数据跨度 {span[0]}–{span[1]}" if span and span[0] else "") + "\n")
    L.append("> 脚本出确定性画像 + 候选规则种子；语义命名与规则定稿由 LLM 在此之上完成。\n")

    L.append("\n## Tier A — 编码指纹（描述型）\n")

    L.append("\n### 1. 需求表达习惯\n")
    L.append(f"- 直接指令 **{ps['n_prompts']}** 条，中位长度 **{ps['median_chars']}** 字符\n")
    L.append(f"- 长度分布：{_inline(ps['length_mix'])}\n")
    L.append(f"- 中文主导度 **{ps['cjk_share']:.0%}** · 带验收/约束 **{ps['acceptance_rate']:.0%}** · "
             f"先讨论倾向 **{ps['discuss_first_rate']:.0%}**\n")

    L.append("\n### 2. 技术栈指纹\n")
    L.append("**AI 实际安装的包（含子 agent 替你装的）**\n")
    L.append(_bar(sf['packages_installed']))
    L.append("\n**你在 prompt 里点名的库/框架**\n")
    L.append(_bar(sf['libs_mentioned']))
    L.append(f"\n**包管理器**：{_inline(sf['package_managers'])}\n")
    L.append(f"**改动最多的文件类型**：{_inline(sf['languages'])}\n")

    L.append("\n### 3. 质量与验证习惯\n")
    L.append(f"- 验证类命令分桶：{_inline(vh['buckets'])}\n")
    L.append(f"- git commit **{vh['commits']}** 次 · 编辑动作 **{vh['edits']}** 次 · "
             f"test:edit 比 **{vh['test_to_edit_ratio']}**\n")
    L.append("> 注：`cmd_key` 只留命令前 2 token，`npm run *` 会被粗归到 build 桶；"
             "直接跑的 pytest/vitest/playwright/tsc/git commit 识别准确。\n")

    L.append("\n### 4. 常做的任务原型\n")
    rc = [c for c in (clusters or {}).get('clusters', []) if not _JUNK_REP.search(c['representative'])]
    if rc:
        for c in rc[:8]:
            rep = ' '.join(c['representative'].split())[:100]   # collapse newlines, cap length
            L.append(f"- （×{c['size']}）{rep} — {', '.join(c.get('top_terms', [])[:5])}\n")
    else:
        L.append("_(暂无 size≥2 的复现任务簇)_\n")

    L.append("\n## Tier B — 候选沉淀规则（memory-ready · 待你确认）\n")
    L.append("> 下面每条都源自**反复出现的摩擦**，建议型不是质问型。确认后可并入 "
             "`memory/` 或 `CLAUDE.md`。LLM 会把它们改写成精准、可执行的规则。\n")
    if ft:
        for t in ft:
            if t['theme'] == '其他':
                continue
            eg = f"（如：{' '.join(t['quotes'][0].split())[:60]}）" if t['quotes'] else ""
            L.append(f"- [ ] **{t['theme']}**（命中 {t['n']} 次）→ 把这条前置进初始 prompt / 规则，"
                     f"省掉后续返工回合 {eg}\n")
    if ps['discuss_first_rate'] >= 0.05:
        L.append("- [ ] **先讨论后写**：你有明显的 text-first 倾向 → 复杂任务默认先给方案/计划再动手\n")
    if not any(t['theme'] != '其他' for t in ft):
        L.append("_(未聚出足够的摩擦主题；样本增多后再看)_\n")

    return ''.join(L)
