"""Secret/PII redaction — applied BEFORE anything is persisted to SQLite."""
import os, re

HOME = os.path.expanduser("~")

_REDACTORS = [
    (re.compile(r'sk-[A-Za-z0-9_\-]{16,}'), '<KEY>'),
    (re.compile(r'(?i)bearer\s+[A-Za-z0-9._\-]{10,}'), 'Bearer <KEY>'),
    (re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'), '<EMAIL>'),
    (re.compile(r'(?i)(api[_\-]?key|token|secret|password)\s*[=:]\s*[^\s"\']{6,}'), r'\1=<REDACTED>'),
    (re.compile(re.escape(HOME)), '~'),
]

def redact(s):
    if not s:
        return s
    for pat, repl in _REDACTORS:
        s = pat.sub(repl, s)
    return s
