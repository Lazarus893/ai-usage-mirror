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
