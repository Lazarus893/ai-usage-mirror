"""Source registry. Add a new AI assistant = add a parser module + one entry here."""
from sources import claude_code, codex

SOURCES = [
    {'name': 'claude-code', 'glob': claude_code.GLOB, 'parse': claude_code.parse},
    {'name': 'codex',       'glob': codex.GLOB,       'parse': codex.parse},
]
