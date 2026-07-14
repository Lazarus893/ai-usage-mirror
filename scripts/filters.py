"""Text filters & normalization — ported verbatim from the validated prototype.
These parameters are hard-won (see ARCHITECTURE.md §4.5); do not loosen without re-verifying."""
import re, collections, datetime
from redact import redact

CJK = re.compile(r'[一-鿿]')
_WORD = re.compile(r'[A-Za-z][A-Za-z0-9_\-]{2,}|[一-鿿]{2,}')

_STOP = set('''the a an to of in on for and or with is are be do i you it this that my me
help please can could would make create add fix update change use using how what why get set
run give write show me my we our just now like need want file code app page 帮 我 你 的 了 吗 呢
一下 这个 那个 请 把 给 用 做 个 想 需要 现在 一个 可以 然后 还有 一些 这里 目前'''.split())

# Harness-injected text that is NOT the user speaking (agent-to-agent / approval reviews / sys preambles).
_INJECTED = (
    "The following is the Codex agent history", "Another Claude session sent",
    "The coordinator sent a message", "<teammate-message", "# Instructions (read first)",
    "prompt injection resistance", "Treat the transcript", "approval assessment",
    "You are Codex", "You are Claude", "system-reminder", "System-Reminder",
    "<system-reminder", "Caveat:", "This session is being", "You are a memory extractor",
)
_IMG = re.compile(r'^\s*(\[Image[^\]]*\]\s*)+')   # leading pasted-image refs / screenshot metadata
# Wrappers that embed a REAL user request further down — strip to the payload.
_UNWRAP = ("## My request for Codex:", "My request for Codex:", "## My request:", "My request:")
_META_PREFIX = ('<', '[Request interrupted', 'Contents of', 'command-', 'API Error', 'FILE UNDER AUDIT')

_CORRECTION = re.compile(
    r'(不是这样|不是我的意思|我是说|不对|错了|重新|你误解|不要这样|别这样|理解错|搞错|不太对|'
    r'还是不|太丑|太多了|信息过载|不够|没对齐|不符合|不友好|没有按照|'
    r'\bactually,?\s|that.?s not|i meant|not what i|no,\s|wrong|revert|undo)', re.I)

def unwrap(text):
    if not text:
        return text
    for m in _UNWRAP:
        i = text.find(m)
        if i != -1:
            text = text[i + len(m):]
            break
    return _IMG.sub('', text).strip()          # strip leading image refs; inline [Image #N] kept

def is_injected(text):
    if not text:
        return True
    head = text.lstrip()[:200]
    return any(m in head for m in _INJECTED)

def is_real_prompt(text):
    if not text:
        return False
    t = text.strip()
    if len(t) < 3:
        return False
    if t.startswith('/'):                 # slash command (/status, /model, ...) — not a prompt
        return False
    for p in _META_PREFIX:
        if t.startswith(p):
            return False
    return True

def is_task_prompt(text):
    """A genuine task request. Kills TUI slash-command artifacts ('status/status', 'atus')."""
    if not is_real_prompt(text):
        return False
    t = text.strip()
    if len(t) < 12:
        return False
    if CJK.search(t):
        return True
    words = [w for w in _WORD.findall(t.lower()) if w not in _STOP]
    return len(set(words)) >= 3           # >=3 DISTINCT words (repeated-token junk -> 1 distinct)

def is_friction(text, prev_asst):
    """Correction/rework signal: must follow an assistant turn, lead with the complaint, be short."""
    return bool(prev_asst and len(text) < 600 and _CORRECTION.search(text[:120]))

def signature(text, k=6):
    words = [w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP and len(w) >= 3]
    top = [w for w, _ in collections.Counter(words).most_common(k)]
    return tuple(sorted(top))

# ---------- project resolution ----------
def project_root(path):
    if not path:
        return None
    p = redact(path.strip().strip('"\''))
    if p in ('~', '/', '.', ''):
        return None
    parts = [x for x in p.split('/') if x]
    if not parts:
        return None
    for anchor in ('Projects', 'Downloads', 'Desktop', 'Documents'):
        if anchor in parts:
            i = parts.index(anchor)
            return '/'.join(parts[:i + 2]) if i + 1 < len(parts) else '/'.join(parts[:i + 1])
    if parts[0] == '~':
        return '~/' + parts[1] if len(parts) >= 2 else None
    return '/' + '/'.join(parts[:2])

def resolve_project(cwd, hints):
    pr = project_root(cwd)
    if pr:
        return pr
    if hints:
        return hints.most_common(1)[0][0]
    return '<none>'

# ---------- command parsing ----------
# cd target = first token after cd: quoted string, or unquoted up to whitespace/redirect/separator.
_CD = re.compile(r'''cd\s+(?:"([^"]+)"|'([^']+)'|([^\s"'&|;<>]+))''')

def cd_targets(cmd):
    return [(m.group(1) or m.group(2) or m.group(3)).strip() for m in _CD.finditer(cmd)]

def command_key(cmd):
    c = re.sub(r'^cd\s+[^&;|]+(&&|;)\s*', '', cmd.strip()).strip()   # drop leading `cd X &&`
    if not c or c.startswith('cd ') or c == 'cd':
        return None                                                  # pure navigation -> not a command
    parts = re.split(r'\s+', c)
    return redact(' '.join(parts[:2]))[:60]

def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone()
    except Exception:
        return None

# ---------- coding-profile signals (deterministic; LLM does the naming/judgment later) ----------
# Package-install command heads -> we keep the TARGET package names (command_key drops them).
_INSTALL = re.compile(
    r'\b(?:'
    r'npm\s+(?:i|install|add)|pnpm\s+(?:add|install|i)|yarn\s+add|bun\s+(?:add|install)|'
    r'(?:python[0-9.]*\s+-m\s+)?pip[0-9]?\s+install|uv\s+(?:add|pip\s+install)|poetry\s+add|'
    r'cargo\s+add|go\s+(?:get|install)|brew\s+install|gem\s+install'
    r')\s+(?P<rest>[^\n&|;]+)', re.I)


def _strip_version(tok):
    """Drop a version specifier from a package token, keeping scoped names intact."""
    if tok.startswith('@'):                          # scoped: @scope/name[@ver]
        at = tok.find('@', 1)
        return tok[:at] if at != -1 else tok
    return re.split(r'@|==|~=|>=|<=|>|<|\[', tok, 1)[0]


def install_pkgs(cmd):
    """Package targets from an install command. Deterministic; drops flags/versions/dupes."""
    if not cmd:
        return []
    out, seen = [], set()
    for m in _INSTALL.finditer(cmd):
        for tok in m.group('rest').split():
            if tok.startswith('-') or tok in ('install', 'add', 'i', 'pip', 'get'):
                continue                             # flags / doubled verbs
            p = _strip_version(tok).strip('"\'`,').lower()
            if not p or not re.search(r'[a-z]', p) or len(p) > 60 or p in seen:
                continue
            if any(c in p for c in '$=;`{}()*') or p[0] in '~./':   # shell noise / paths / vars
                continue
            seen.add(p)
            out.append(p)
    return out


# Package managers we recognize as a command head (filters compound-command noise like `mkdir`).
KNOWN_MGR = {'npm', 'pnpm', 'yarn', 'bun', 'npx', 'pip', 'pip3', 'uv', 'poetry',
             'cargo', 'go', 'brew', 'gem'}


def mgr_head(cmd_key):
    """The package-manager head of a cmd_key, or None (rejects mkdir/ssh/… noise)."""
    if not cmd_key:
        return None
    head = cmd_key.split()[0].lower()
    return head if head in KNOWN_MGR else None


# Curated library lexicon (aligned to the user's stack + common web/py). Prompt-mention signal.
LIB_LEXICON = {
    'react', 'next.js', 'next', 'vue', 'svelte', 'solid', 'angular', 'astro',
    'tailwind', 'shadcn', 'radix', 'chakra', 'mui', 'assistant-ui',
    'vite', 'webpack', 'turbopack', 'esbuild', 'rollup', 'turborepo',
    'framer motion', 'framer-motion', 'gsap', 'anime.js', 'animejs', 'three.js', 'three',
    'd3', 'p5.js', 'p5', 'recharts', 'visx',
    'lexical', 'codemirror', 'tiptap', 'prosemirror',
    'bullmq', 'redis', 'postgres', 'drizzle', 'prisma', 'sqlite', 'mongodb', 'supabase',
    'trpc', 'better auth', 'better-auth', 'clerk', 'nextauth', 'zod', 'zustand', 'jotai',
    'redux', 'react-query', 'tanstack', 'playwright', 'vitest', 'jest', 'cypress',
    'biome', 'eslint', 'prettier', 'ruff', 'mypy',
    'pi-ai', 'langchain', 'anthropic', 'openai', 'ollama',
    'fastapi', 'flask', 'django', 'express', 'hono', 'fastify',
    'pandas', 'numpy', 'pytorch', 'tensorflow', 'onnxruntime', 'fastembed', 'openpyxl',
    'docker', 'kubernetes', 'terraform',
}
_LIB_RE = re.compile(
    r'(?<![\w.-])(' + '|'.join(sorted((re.escape(l) for l in LIB_LEXICON), key=len, reverse=True)) +
    r')(?![\w.-])', re.I)
# Fold near-duplicate spellings into one canonical name.
LIB_ALIAS = {'next': 'next.js', 'three': 'three.js', 'animejs': 'anime.js', 'p5': 'p5.js',
             'framer-motion': 'framer motion', 'better-auth': 'better auth'}


def libs_in_text(text):
    """Canonical library names mentioned in a user prompt (deduped, aliases folded)."""
    if not text:
        return []
    out, seen = [], set()
    for m in _LIB_RE.finditer(text.lower()):
        v = LIB_ALIAS.get(m.group(1), m.group(1))
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# Verification/quality-habit buckets over cmd_key (which is only the first 2 tokens — coarse).
_VERIF = [
    ('test', re.compile(r'\b(pytest|vitest|jest|cypress|playwright|unittest|npm\s+test|'
                        r'yarn\s+test|pnpm\s+test|go\s+test|cargo\s+test)', re.I)),
    ('git', re.compile(r'^git\b', re.I)),
    ('typecheck', re.compile(r'\b(tsc|mypy|pyright|eslint|biome|ruff|flake8|prettier)\b', re.I)),
    ('build', re.compile(r'\b(make|webpack|vite|next|cargo\s+build|uvicorn|npm\s+run)\b', re.I)),
]


def verif_bucket(cmd_key):
    """Map a command head to a quality-habit bucket, or None. Coarse (cmd_key is truncated)."""
    if not cmd_key:
        return None
    for name, rx in _VERIF:
        if rx.search(cmd_key):
            return name
    return None


# Prompting-style markers (operate on user message text).
_ACCEPTANCE = re.compile(
    r'(验收|确保|必须|别忘|不要|别改|保持|跑通|测一下|跑一下测试|通过测试|符合|不能|'
    r'acceptance|make sure|must\b|do ?n.?t\b|ensure|verify that|should not)', re.I)
_DISCUSS = re.compile(
    r'(先讨论|先别写|别急着写|先说思路|先给方案|先聊|讨论清楚|先不要动手|先出.{0,4}计划|'
    r'do ?n.?t code yet|let.?s discuss|before you (write|code|start))', re.I)


def has_acceptance(text):
    return bool(text and _ACCEPTANCE.search(text))


def wants_discussion(text):
    return bool(text and _DISCUSS.search(text))
