"""Build digest.json from the SQLite store (ARCHITECTURE.md §5). Derived, rebuildable.

Layered kind filtering (see M1 nuance):
  - engagement metrics (sessions / temporal / projects / friction / task_prompts): kind='real' only
  - work metrics (tools / commands / filetypes / models / tokens): kind IN ('real','meta')
    (meta includes sub-agent sidechains — delegated work that is still 'yours')
  - artifact sessions (TUI /status junk) are excluded from everything.
"""
import collections
import filters as F

WORK = ('real', 'meta')

TOKENS_NOTE = ("output incl. reasoning; Codex token_count only in recent sessions (partial coverage); "
               "models counted per-session (dominant), not per-turn")


def _counter_rows(rows):
    return {k: v for k, v in rows}


def build_digest(db):
    # --- session kind breakdown ---
    kind_rows = db.execute("SELECT source, kind, COUNT(*) FROM session GROUP BY source, kind").fetchall()
    by_kind = collections.defaultdict(dict)
    for src, kind, n in kind_rows:
        by_kind[src][kind] = n
    per_source = {s: k.get('real', 0) for s, k in by_kind.items()}
    meta_skipped = {s: k.get('meta', 0) for s, k in by_kind.items()}
    artifact_skipped = {s: k.get('artifact', 0) for s, k in by_kind.items()}
    total_real = sum(per_source.values()) or 1

    # --- temporal (real, user msgs, LOCAL time via parse_ts) ---
    by_hour = [0] * 24
    by_weekday = [0] * 7
    span_min = span_max = None
    for (ts,) in db.execute(
            "SELECT m.ts FROM message m JOIN session s ON m.session_id=s.id "
            "WHERE s.kind='real' AND m.role='user' AND m.ts IS NOT NULL"):
        dt = F.parse_ts(ts)
        if not dt:
            continue
        by_hour[dt.hour] += 1
        by_weekday[dt.weekday()] += 1
        if span_min is None or dt < span_min:
            span_min = dt
        if span_max is None or dt > span_max:
            span_max = dt

    ph = ','.join('?' * len(WORK))

    # --- projects (work metric: real+meta, since sub-agent sidechains carry the true workdir) ---
    total_work = db.execute(
        f"SELECT COUNT(*) FROM session WHERE kind IN ({ph})", WORK).fetchone()[0] or 1
    projects = [{'cwd': p, 'sessions': n, 'share': round(n / total_work, 3)}
                for p, n in db.execute(
            f"SELECT project, COUNT(*) c FROM session WHERE kind IN ({ph}) "
            f"GROUP BY project ORDER BY c DESC LIMIT 15", WORK)]

    # --- work metrics (real+meta) ---
    tools = _counter_rows(db.execute(
        f"SELECT tool, COUNT(*) c FROM tool_call t JOIN session s ON t.session_id=s.id "
        f"WHERE s.kind IN ({ph}) GROUP BY tool ORDER BY c DESC LIMIT 25", WORK))
    commands = [{'cmd': c, 'n': n} for c, n in db.execute(
        f"SELECT cmd_key, COUNT(*) c FROM tool_call t JOIN session s ON t.session_id=s.id "
        f"WHERE s.kind IN ({ph}) AND cmd_key IS NOT NULL GROUP BY cmd_key ORDER BY c DESC LIMIT 25", WORK)]
    filetypes = _counter_rows(db.execute(
        f"SELECT file_ext, COUNT(*) c FROM tool_call t JOIN session s ON t.session_id=s.id "
        f"WHERE s.kind IN ({ph}) AND file_ext IS NOT NULL GROUP BY file_ext ORDER BY c DESC LIMIT 15", WORK))
    models = _counter_rows(db.execute(
        f"SELECT model, COUNT(*) c FROM session WHERE kind IN ({ph}) AND model IS NOT NULL "
        f"GROUP BY model ORDER BY c DESC", WORK))
    tokens = _counter_rows(db.execute(
        f"SELECT source, SUM(out_tokens) FROM session WHERE kind IN ({ph}) GROUP BY source", WORK))

    # --- friction (real, dedup exact quote) ---
    friction = []
    seen = set()
    for sid, source, text in db.execute(
            "SELECT s.id, s.source, m.text FROM message m JOIN session s ON m.session_id=s.id "
            "WHERE s.kind='real' AND m.is_friction=1 AND m.text IS NOT NULL ORDER BY m.ts"):
        q = text[:180]
        if q in seen:
            continue
        seen.add(q)
        friction.append({'source': source, 'session': sid, 'quote': q})

    # --- task_prompts (real, dedup by sig, keep count) ---
    by_sig = {}
    for sid, source, text, sig in db.execute(
            "SELECT s.id, s.source, m.text, m.sig FROM message m JOIN session s ON m.session_id=s.id "
            "WHERE s.kind='real' AND m.is_task=1 AND m.sig IS NOT NULL AND m.text IS NOT NULL"):
        if sig in by_sig:
            by_sig[sig]['n'] += 1
        else:
            by_sig[sig] = {'prompt': text[:160], 'n': 1, 'source': source}
    task_prompts = sorted(by_sig.values(), key=lambda x: -x['n'])[:60]

    return {
        'meta': {
            'sources': sorted(by_kind.keys()),
            'sessions': total_real,
            'meta_sessions_skipped': meta_skipped,
            'artifact_sessions_skipped': artifact_skipped,
            'tokens_note': TOKENS_NOTE,
            'span': [span_min.date().isoformat() if span_min else None,
                     span_max.date().isoformat() if span_max else None],
        },
        'temporal': {'by_hour': by_hour, 'by_weekday': by_weekday},
        'projects': projects,
        'tools_used': tools,
        'commands_top': commands,
        'edited_filetypes': filetypes,
        'models': models,
        'output_tokens_est': {k: (v or 0) for k, v in tokens.items()},
        'friction_candidates': friction[:40],
        'task_prompts': task_prompts,
        'per_source': per_source,
    }
