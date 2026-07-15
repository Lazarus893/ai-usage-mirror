#!/usr/bin/env python3
"""把 ai-usage-mirror 的记录蒸馏成「元宝输入法」(Handy)可直接吃的 profile 工件。

各管一段:本脚本只负责「导出工件」(镜子是源头),元宝输入法侧负责「导入落库」。
输入 = 镜子已产出的 `.state/digest.json`(≤30KB,已脱敏)+(可选)`.state/mirror.db`
        里蒸馏出的编码指纹(stack_fingerprint / prompting_style);原始 transcript 不参与。
输出 = `.state/input_profile.json`(schema v2),拆成两条 Handy 原生通道:

  · terms —— 用户领域高频专名(项目名 / CamelCase 库名 / 缩写 / 高频中文词)
             → 输入法**词典 hotword**,直接改善 ASR 对专名的识别与 refinement 的书面化。
             (这条是「上下文」通道,靠字面命中,宁缺毋滥。)
  · facts —— 从真实使用史归纳的**结构化个人记忆**(topic + fact):技术栈 / 主力语言 /
             常做项目 / 协作风格。→ 输入法 **memory_facts**,给 refinement 定调、给 VP
             c-mode 的「项目 Prompt」policy 一份**有数据支撑**的工程约束先验
             (替代 Handy 里原来硬编码的 demo seed)。
  · topics —— 旧版 v1 字段(项目占比句),保留仅为向后兼容老 Handy;新 Handy 有 facts 时忽略它。

刻意做「确定性抽取」(脚本干确定性,判断归 LLM);高精度优先、宁缺毋滥。全程本地、只读、不联网。

用法:
  python3 scripts/export_input_profile.py            # 读 digest + mirror.db,写 input_profile.json
  python3 scripts/export_input_profile.py --digest <path> --db <path> --out <path>
  python3 scripts/export_input_profile.py --no-db    # 只用 digest(缺 DB 时自动降级,不强依赖)
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

# ── terms 侧停用词(同 v1,承接词典 hotword 通道) ──────────────────────────
# 通用中文词:高频但非专名,进词典只会污染。宁可多列。
CJK_STOP = {
    "这个", "那个", "一下", "一个", "问题", "分析", "代码", "目前", "现在", "已经",
    "可以", "需要", "应该", "帮我", "你的", "我的", "我们", "他们", "什么", "怎么",
    "为什么", "如果", "因为", "所以", "但是", "而且", "然后", "首先", "其次", "最后",
    "基于", "对比", "详细", "整体", "部分", "内容", "功能", "实现", "设计", "优化",
    "修改", "检查", "生成", "创建", "使用", "支持", "数据", "模型", "文件", "页面",
    "组件", "版本", "时间", "上面", "下面", "左侧", "右侧", "报错", "完整", "直接",
    "重新", "一起", "还是", "感觉", "觉得", "确认", "继续", "增加", "删除", "调整",
    "一样", "不是", "没有", "这里", "这样", "看一下", "帮我看", "是不是", "这些",
    "以及", "或者", "这段", "这句", "一点", "东西", "方案", "逻辑", "结构", "情况",
    "问题的", "的话", "一版", "目录", "字段", "接口", "服务", "配置", "参数", "结果",
    "对话", "输入", "项目", "测试", "建议", "记忆", "用户", "另外", "下这", "名字",
    "时候", "开始", "完成", "运行", "输出", "格式", "样式", "颜色", "布局", "交互",
    "字段的", "一段", "一句", "一条", "地方", "样子", "过程", "状态", "信息", "系统",
}
# 通用英文/技术词:太泛,不作专名。较窄,GT/MCP/DeepOps 等领域词故意放行。
EN_STOP = {
    "API", "JSON", "HTML", "CSS", "HTTP", "HTTPS", "URL", "URI", "SQL", "UI", "UX",
    "ID", "OK", "TODO", "FIXME", "README", "AI", "APP", "APIs", "DOM", "CDN", "OS",
    "PDF", "PNG", "JPG", "CSV", "YAML", "XML", "The", "This", "That", "And",
    "Projects", "SKILL", "Star", "Office", "Continue", "Please", "With",
    # skill / 样板 prompt 里的英文误提(Base directory / # Instructions / # User request / RULE …)
    "Base", "User", "Instructions", "Instruction", "Directory", "Request",
    "Rule", "Override", "Response", "Requirements", "Outputs", "Section",
}
CJK_RE = re.compile(r"[一-鿿]{2,}")
# CamelCase(BullMQ/Lexical)/ 全大写缩写(MCP/GT/DeepOps 里的 DeepOps 走 CamelCase)。
EN_RE = re.compile(
    r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+|[A-Z]{2,6}|[A-Z][a-z]{2,})\b"
)

CJK_MIN = 10     # 中文 n-gram 最低加权频次(高阈值保精度)
EN_MIN = 3       # 英文专名最低加权频次
MAX_TERMS = 60   # 词典别灌太多,取头部
MAX_TOPICS = 4
MAX_FACTS = 14   # Handy refinement 侧 MAX_MEMORY_ITEMS=20,留余量给用户自己加的记忆


def load_digest(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"找不到 digest:{path}\n先跑 `python3 scripts/mirror.py ingest && "
            f"python3 scripts/mirror.py digest --json > .state/digest.json`,或用 --digest 指定。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_profile(db_path: Path):
    """从 mirror.db 蒸馏 stack_fingerprint / prompting_style / verification_habits。
    缺库或依赖不全时返回 None(降级到 digest-only),绝不让导出失败。"""
    if not db_path.exists():
        return None
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    try:
        from store import Store  # noqa: WPS433 (本地依赖,延迟导入以支持 --no-db)
        import profile as profile_mod
    except Exception as exc:  # pragma: no cover - 依赖缺失时的兜底
        print(f"[export] 读 mirror.db 失败,降级到 digest-only:{exc}", file=sys.stderr)
        return None
    try:
        st = Store(str(db_path))
        st.init_schema()
        return profile_mod.build_profile(st.db)
    except Exception as exc:  # pragma: no cover
        print(f"[export] build_profile 失败,降级到 digest-only:{exc}", file=sys.stderr)
        return None


# ── terms(词典 hotword)──────────────────────────────────────────────────
# 通用/系统目录 basename:不是真项目名,当专名或项目 fact 都是噪声。
_GENERIC_BASE = {
    "codex", "downloads", "projects", "desktop", "documents", "none", "tmp", "src", "clawd",
    "library", "applications", "movies", "music", "pictures", "public", "home", "users",
}
_FILE_EXT_RE = re.compile(r"\.(app|md|json|html?|py|ts|js|mjs|txt|png|jpg|csv)$", re.I)


def clean_basename(cwd: str):
    """cwd → 干净仓库名;路径碎片 / dotfile / 文件式目录 / 通用名 一律丢。"""
    cwd = (cwd or "").strip()
    if not cwd or cwd == "<none>":
        return None
    base = cwd.rstrip("/").split("/")[-1]
    if len(base) < 2 or base.startswith(".") or _FILE_EXT_RE.search(base):
        return None
    if base.lower() in _GENERIC_BASE:
        return None
    return base


def project_terms(digest: dict) -> Counter:
    """项目 basename → 干净、高置信的专名。"""
    out = Counter()
    for p in digest.get("projects", []):
        base = clean_basename(p.get("cwd", ""))
        if base:
            out[base] += int(p.get("sessions", 1))
    return out


def prompt_terms(digest: dict):
    """从 task_prompts 抽英文专名 + 中文高频 n-gram(加权 by 出现次数 n)。"""
    en = Counter()
    cjk = Counter()
    for tp in digest.get("task_prompts", []):
        text = tp.get("prompt") or ""
        n = int(tp.get("n", 1))
        for m in EN_RE.findall(text):
            if m not in EN_STOP and 2 <= len(m) <= 30:
                en[m] += n
        for seg in CJK_RE.findall(text):
            seen_in_seg = set()
            for length in (4, 3, 2):
                for i in range(len(seg) - length + 1):
                    gram = seg[i : i + length]
                    if gram in CJK_STOP or gram in seen_in_seg:
                        continue
                    cjk[gram] += n
                    seen_in_seg.add(gram)
    return en, cjk


def dedup_longest(cjk: Counter):
    """最长匹配去冗:若短词是已收长词的子串且频次相近(≤1.5x),丢短词。"""
    kept = []
    for gram, cnt in sorted(cjk.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        if cnt < CJK_MIN:
            continue
        redundant = any(
            gram in longer and cnt <= kcnt * 1.5 for longer, kcnt in kept
        )
        if not redundant:
            kept.append((gram, cnt))
    return kept


def build_terms(digest: dict):
    terms = Counter()
    terms.update(project_terms(digest))
    en, cjk = prompt_terms(digest)
    for term, cnt in en.items():
        if cnt >= EN_MIN:
            terms[term] += cnt
    for gram, cnt in dedup_longest(cjk):
        terms[gram] += cnt
    return [{"term": t, "weight": w} for t, w in terms.most_common(MAX_TERMS)]


# ── facts(结构化个人记忆)────────────────────────────────────────────────
# 技术栈映射:把 stack_fingerprint 里的规范库名 / npm 包名折叠成人读展示名 + 分组。
# 只白名单有意义的栈信号,helper/工具噪声(clsx / tailwind-merge / nanoid / jsdom …)一律丢。
# key = 观测到的小写 token(canonical 或 package 名),value = (分组, 展示名)。
_STACK = {
    # 前端框架 / UI / 动效
    "react": ("前端/UI", "React"), "next.js": ("前端/UI", "Next.js"), "next": ("前端/UI", "Next.js"),
    "vue": ("前端/UI", "Vue"), "svelte": ("前端/UI", "Svelte"), "solid": ("前端/UI", "Solid"),
    "angular": ("前端/UI", "Angular"), "astro": ("前端/UI", "Astro"),
    "tailwind": ("前端/UI", "Tailwind"), "shadcn": ("前端/UI", "shadcn/ui"),
    "radix": ("前端/UI", "Radix"), "chakra": ("前端/UI", "Chakra"), "mui": ("前端/UI", "MUI"),
    "framer motion": ("前端/UI", "Framer Motion"), "framer-motion": ("前端/UI", "Framer Motion"),
    "gsap": ("前端/UI", "GSAP"), "anime.js": ("前端/UI", "anime.js"), "animejs": ("前端/UI", "anime.js"),
    "three.js": ("前端/UI", "Three.js"), "three": ("前端/UI", "Three.js"),
    "d3": ("前端/UI", "D3"), "p5.js": ("前端/UI", "p5.js"), "recharts": ("前端/UI", "Recharts"),
    "visx": ("前端/UI", "visx"), "zustand": ("前端/UI", "Zustand"), "jotai": ("前端/UI", "Jotai"),
    "redux": ("前端/UI", "Redux"), "assistant-ui": ("前端/UI", "assistant-ui"),
    "vite": ("前端/UI", "Vite"),
    # 富文本 / 编辑器
    "lexical": ("编辑器", "Lexical"), "codemirror": ("编辑器", "CodeMirror"),
    "tiptap": ("编辑器", "Tiptap"), "prosemirror": ("编辑器", "ProseMirror"),
    # 数据 / 后端
    "postgres": ("数据/后端", "Postgres"), "drizzle": ("数据/后端", "Drizzle"),
    "drizzle-orm": ("数据/后端", "Drizzle"), "prisma": ("数据/后端", "Prisma"),
    "sqlite": ("数据/后端", "SQLite"), "mongodb": ("数据/后端", "MongoDB"),
    "supabase": ("数据/后端", "Supabase"), "trpc": ("数据/后端", "tRPC"),
    "zod": ("数据/后端", "Zod"), "redis": ("数据/后端", "Redis"), "bullmq": ("数据/后端", "BullMQ"),
    "fastapi": ("数据/后端", "FastAPI"), "flask": ("数据/后端", "Flask"),
    "django": ("数据/后端", "Django"), "express": ("数据/后端", "Express"),
    "hono": ("数据/后端", "Hono"), "fastify": ("数据/后端", "Fastify"),
    "better auth": ("数据/后端", "Better Auth"),
    # 测试 / 质量
    "playwright": ("测试/质量", "Playwright"), "playwright-core": ("测试/质量", "Playwright"),
    "vitest": ("测试/质量", "Vitest"), "jest": ("测试/质量", "Jest"),
    "cypress": ("测试/质量", "Cypress"), "biome": ("测试/质量", "Biome"),
    "eslint": ("测试/质量", "ESLint"), "prettier": ("测试/质量", "Prettier"),
    "ruff": ("测试/质量", "Ruff"), "mypy": ("测试/质量", "mypy"),
    # AI / Agent
    "anthropic": ("AI/Agent", "Anthropic SDK"), "openai": ("AI/Agent", "OpenAI SDK"),
    "pi-ai": ("AI/Agent", "pi SDK"), "langchain": ("AI/Agent", "LangChain"),
    "ollama": ("AI/Agent", "Ollama"),
    # 桌面 / 基础设施
    "tauri": ("桌面/基建", "Tauri"), "docker": ("桌面/基建", "Docker"),
    "kubernetes": ("桌面/基建", "Kubernetes"), "terraform": ("桌面/基建", "Terraform"),
    # 数据处理
    "pandas": ("数据处理", "pandas"), "numpy": ("数据处理", "NumPy"),
    "pytorch": ("数据处理", "PyTorch"), "tensorflow": ("数据处理", "TensorFlow"),
    "onnxruntime": ("数据处理", "ONNX Runtime"), "openpyxl": ("数据处理", "openpyxl"),
}
# 分组展示顺序(稳定输出)。
_STACK_GROUP_ORDER = ["前端/UI", "编辑器", "数据/后端", "测试/质量", "AI/Agent", "桌面/基建", "数据处理"]
# 采信阈值分来源:AI 实际**安装**过的包 ≥1 次就算强信号(装了就用了);
# 只在 prompt 里**提到**的库要 ≥2 次(单次提及可能只是随口一说)。
_STACK_MENTION_MIN = 2
_STACK_INSTALL_MIN = 1
_MAX_PER_GROUP = 8

# 文件扩展名 → 语言/技术展示名(None = 非代码,忽略)。
_EXT_LANG = OrderedDict([
    (".ts", "TypeScript"), (".tsx", "TypeScript"), (".js", "JavaScript"),
    (".mjs", "JavaScript"), (".cjs", "JavaScript"), (".html", "HTML/前端"),
    (".css", "CSS"), (".py", "Python"), (".rs", "Rust"), (".swift", "Swift"),
    (".go", "Go"), (".java", "Java"), (".kt", "Kotlin"), (".rb", "Ruby"),
    (".php", "PHP"), (".c", "C"), (".cpp", "C++"), (".sql", "SQL"), (".sh", "Shell"),
])


def _stack_facts(profile: dict, digest: dict):
    """技术栈 facts:合并 libs_mentioned(你 prompt 点名)+ packages_installed(AI 实际装),
    折叠成分组的人读记忆。有数据支撑,直接给 VP 的项目-Prompt policy 当工程约束先验。"""
    mentions = Counter()   # prompt 点名
    installs = Counter()   # AI 实际安装
    if profile:
        sf = profile.get("stack_fingerprint", {})
        for item in sf.get("libs_mentioned", []):
            mentions[item["name"].lower()] += int(item.get("n", 1))
        for item in sf.get("packages_installed", []):
            installs[item["name"].lower()] += int(item.get("n", 1))

    # 只保留达标 token,按「安装权重高于提及」的综合分排序(装了的排前面)。
    def qualifies(tok):
        return installs[tok] >= _STACK_INSTALL_MIN or mentions[tok] >= _STACK_MENTION_MIN

    tokens = {t for t in set(mentions) | set(installs) if qualifies(t)}
    ranked = sorted(tokens, key=lambda t: -(installs[t] * 3 + mentions[t]))

    groups = OrderedDict((g, []) for g in _STACK_GROUP_ORDER)
    seen_display = set()
    for token in ranked:
        hit = _STACK.get(token)
        if not hit:
            continue
        group, display = hit
        if display in seen_display:
            continue
        if len(groups[group]) >= _MAX_PER_GROUP:
            continue
        groups[group].append(display)
        seen_display.add(display)

    facts = []
    for group, members in groups.items():
        if members:
            facts.append({
                "topic": "技术栈",
                "fact": f"{group}常用:{' · '.join(members)}(据本地 AI 使用记录归纳)",
            })
    return facts


def _language_fact(profile: dict, digest: dict):
    """主力语言 fact:优先 stack_fingerprint.languages,退回 digest.edited_filetypes。"""
    ext_counts = {}
    if profile:
        ext_counts = profile.get("stack_fingerprint", {}).get("languages", {}) or {}
    if not ext_counts:
        ext_counts = digest.get("edited_filetypes", {}) or {}
    if not ext_counts:
        return None
    ranked = []
    seen = set()
    for ext, _cnt in sorted(ext_counts.items(), key=lambda kv: -int(kv[1])):
        lang = _EXT_LANG.get(ext.lower())
        if lang and lang not in seen:
            seen.add(lang)
            ranked.append(lang)
        if len(ranked) >= 5:
            break
    if not ranked:
        return None
    return {"topic": "语言", "fact": f"主力技术/语言:{' · '.join(ranked)}"}


def _projects_fact(digest: dict):
    """常做项目 fact:top 几个干净项目名折成一条。"""
    names = []
    for p in sorted(digest.get("projects", []), key=lambda x: -float(x.get("share", 0))):
        base = clean_basename(p.get("cwd", ""))
        if base and base not in names and float(p.get("share", 0)) >= 0.02:
            names.append(base)
        if len(names) >= 5:
            break
    if not names:
        return None
    return {"topic": "项目", "fact": f"近期主要在这些项目上工作:{' · '.join(names)}"}


def _style_facts(profile: dict):
    """协作风格 facts:直接影响 refinement 定调 + VP 生成语域。仅在信号明确时发。"""
    if not profile:
        return []
    ps = profile.get("prompting_style", {})
    facts = []
    if ps.get("cjk_share", 0) >= 0.7:
        facts.append({"topic": "协作风格",
                      "fact": "习惯用中文下达指令,书面化/润色应保持中文表达"})
    mix = ps.get("length_mix", {}) or {}
    total = sum(mix.values()) or 1
    terse_share = mix.get("terse (<40)", 0) / total
    if terse_share >= 0.4 or (ps.get("median_chars") or 999) < 40:
        facts.append({"topic": "协作风格",
                      "fact": "指令偏简短直接,常用一句话表达需求"})
    if ps.get("acceptance_rate", 0) >= 0.1:
        facts.append({"topic": "协作风格",
                      "fact": "布置任务时常附明确的验收标准或约束条件"})
    if ps.get("discuss_first_rate", 0) >= 0.05:
        facts.append({"topic": "协作风格",
                      "fact": "复杂任务倾向先讨论方案再动手实现"})
    return facts


def build_facts(profile: dict, digest: dict):
    """把四类信号拼成有序、去重、有界的 memory facts。技术栈优先(对 VP 最有用),风格垫底。"""
    facts = []
    facts.extend(_stack_facts(profile, digest))
    lang = _language_fact(profile, digest)
    if lang:
        facts.append(lang)
    proj = _projects_fact(digest)
    if proj:
        facts.append(proj)
    facts.extend(_style_facts(profile))

    # 去重(按 fact 文本)并截断。
    out, seen = [], set()
    for f in facts:
        key = f["fact"]
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
        if len(out) >= MAX_FACTS:
            break
    return out


def build_topics(digest: dict):
    """v1 legacy 字段:项目占比句。仅为老 Handy 向后兼容保留。"""
    topics = []
    for p in digest.get("projects", []):
        share = float(p.get("share", 0))
        base = clean_basename(p.get("cwd", ""))
        if not base or share < 0.03:
            continue
        topics.append(f"经常在「{base}」项目上工作(AI 会话占比约 {round(share * 100)}%)")
        if len(topics) >= MAX_TOPICS:
            break
    return topics


def main() -> None:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", default=str(here / ".state" / "digest.json"))
    ap.add_argument("--db", default=str(here / ".state" / "mirror.db"))
    ap.add_argument("--out", default=str(here / ".state" / "input_profile.json"))
    ap.add_argument("--no-db", action="store_true",
                    help="不读 mirror.db,只用 digest(facts 降级为 语言/项目)")
    args = ap.parse_args()

    digest = load_digest(Path(args.digest))
    profile = None if args.no_db else load_profile(Path(args.db))

    profile_out = {
        "schema": "yuanbao-input-profile/v2",
        "source": "ai-usage-mirror",
        "terms": build_terms(digest),
        "facts": build_facts(profile, digest),
        "topics": build_topics(digest),  # v1 兼容;新 Handy 有 facts 时忽略
    }

    out = Path(args.out)
    out.write_text(json.dumps(profile_out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人看的摘要走 stderr(stdout 留给可能的管道)。
    deg = " (digest-only 降级)" if profile is None else ""
    print(
        f"[export] terms={len(profile_out['terms'])} facts={len(profile_out['facts'])} "
        f"topics={len(profile_out['topics'])}{deg} → {out}",
        file=sys.stderr,
    )
    print("[export] 头部专名: " + " · ".join(t["term"] for t in profile_out["terms"][:20]),
          file=sys.stderr)
    for f in profile_out["facts"]:
        print(f"[export] fact · [{f['topic']}] {f['fact']}", file=sys.stderr)


if __name__ == "__main__":
    main()
