#!/usr/bin/env python3
"""ai-usage-mirror CLI. Discipline: --json -> stdout only data; progress/diagnostics -> stderr.
M1 implements: ingest (incremental), stats. (digest/cluster/pack/search land in M2-M4.)"""
import os, sys, glob, json, time, argparse, collections

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from store import Store
from sources import SOURCES
import aggregate
import cluster

STATE_DIR = os.path.normpath(os.path.join(HERE, '..', '.state'))
DB_PATH = os.path.join(STATE_DIR, 'mirror.db')
DIGEST_PATH = os.path.join(STATE_DIR, 'digest.json')

# Semantic exit codes (ARCHITECTURE.md §8)
OK, HEALTH_FAIL, EMPTY, CORRUPT, VERSION_MISMATCH = 0, 1, 3, 5, 6

from store import SCHEMA_VERSION


def log(*a):
    print(*a, file=sys.stderr)


def open_store():
    st = Store(DB_PATH)
    st.init_schema()
    return st


def guard(st):
    """Preflight for data commands. Exits with a semantic code + stderr hint if not ready."""
    sv = st.stored_version()
    if sv is not None and sv != SCHEMA_VERSION:
        log(f"[guard] schema mismatch: db={sv} expected={SCHEMA_VERSION} -> run: mirror ingest --full")
        st.close()
        sys.exit(VERSION_MISMATCH)
    if st.session_count() == 0:
        log("[guard] empty store -> run: mirror ingest")
        st.close()
        sys.exit(EMPTY)


def cmd_ingest(args):
    st = open_store()
    if args.full:
        st.drop_all()                                # true clean rebuild (schema + data) -> re-stamps version
        st.init_schema()
        log("[ingest] --full: dropped all tables, rebuilding")
    t0 = time.time()
    status = collections.Counter()
    for src in SOURCES:
        files = sorted(glob.glob(os.path.expanduser(src['glob']), recursive=True))
        for i, f in enumerate(files):
            try:
                status[src['name'] + ':' + st.ingest_file(src['name'], f, src['parse'])] += 1
            except Exception as e:
                status[src['name'] + ':error'] += 1
                log(f"  ! {os.path.basename(f)}: {e}")
            if i % 200 == 0:
                log(f"  {src['name']} {i}/{len(files)}")
    dt = time.time() - t0
    stats = st.stats()
    st.close()
    log(f"[ingest] done in {dt:.2f}s  status={dict(status)}")
    print(json.dumps({'elapsed_s': round(dt, 2), 'status': dict(status),
                      'sessions_by_source_kind': stats}, ensure_ascii=False))


def cmd_stats(args):
    st = open_store()
    stats = st.stats()
    real = {s: k.get('real', 0) for s, k in stats.items()}
    st.close()
    print(json.dumps({'sessions_by_source_kind': stats, 'real': real}, ensure_ascii=False, indent=2))


def cmd_digest(args):
    st = open_store()
    guard(st)
    digest = aggregate.build_digest(st.db)
    st.close()
    with open(DIGEST_PATH, 'w') as fh:
        json.dump(digest, fh, ensure_ascii=False, indent=2)
    log(f"[digest] {os.path.getsize(DIGEST_PATH)/1024:.1f} KB -> {DIGEST_PATH}")
    print(json.dumps(digest, ensure_ascii=False, indent=2 if args.pretty else None))


def cmd_report(args):
    st = open_store()
    guard(st)
    digest = aggregate.build_digest(st.db)
    clusters = cluster.cluster_tasks(st.db, use_embeddings=args.embeddings)
    st.close()
    import datetime
    digest.setdefault('meta', {})['generated'] = datetime.datetime.now().isoformat(timespec='minutes')
    data = json.dumps({'digest': digest, 'clusters': clusters}, ensure_ascii=False)
    tpl_path = os.path.join(HERE, 'report_template.html')
    with open(tpl_path, encoding='utf-8') as fh:
        html = fh.read().replace('__MIRROR_DATA__', data)
    out = os.path.join(STATE_DIR, 'report.html')
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(html)
    log(f"[report] {os.path.getsize(out)/1024:.0f} KB -> {out}")
    print(out)
    if not args.no_open:                       # auto-open by default (the report is meant to be reviewed)
        import subprocess
        subprocess.run(['open', out], check=False)


def cmd_cluster(args):
    st = open_store()
    guard(st)
    res = cluster.cluster_tasks(st.db, use_embeddings=args.embeddings, threshold=args.threshold)
    st.close()
    if args.embeddings and res['mode'] != 'embeddings':
        log("[cluster] fastembed unavailable -> fell back to TF-IDF (pip install fastembed to enable)")
    log(f"[cluster] mode={res['mode']} tasks={res['n_tasks']} "
        f"clusters={len(res['clusters'])} singletons={res.get('n_singletons')}")
    print(json.dumps(res, ensure_ascii=False, indent=2 if args.pretty else None))


def cmd_search(args):
    st = open_store()
    guard(st)
    hits = st.search(args.query, limit=args.limit)
    st.close()
    print(json.dumps({'query': args.query, 'hits': hits}, ensure_ascii=False))


def _est_tokens(s):
    return max(1, len(s) // 3)                       # rough; CJK-heavy ~2-3 chars/token


def cmd_pack(args):
    st = open_store()
    guard(st)
    hits = st.search(args.query, limit=200)
    st.close()
    budget = args.max_tokens
    used = 0
    items = []
    for h in hits:
        ex = (h['text'] or '')[:500]
        cost = _est_tokens(ex)
        if used + cost > budget:
            break
        used += cost
        items.append({'session': h['session'], 'source': h['source'],
                      'project': h['project'], 'excerpt': ex})
    print(json.dumps({'query': args.query, 'max_tokens': budget, 'used_tokens_est': used,
                      'truncated': len(items) < len(hits), 'items': items}, ensure_ascii=False))


CAPABILITIES = {
    'version': SCHEMA_VERSION,
    'exit_codes': {'0': 'ok', '1': 'health failure', '3': 'empty (run ingest)',
                   '5': 'corruption', '6': 'schema mismatch (run ingest --full)'},
    'discipline': 'with --json: stdout=data only, stderr=progress/diagnostics',
    'commands': [
        {'cmd': 'triage', 'purpose': 'safe first call: report readiness + next_command', 'json': True},
        {'cmd': 'ingest [--full]', 'purpose': 'parse sources into SQLite (incremental)', 'json': True},
        {'cmd': 'digest [--pretty]', 'purpose': 'rebuild the aggregate digest for the report', 'json': True},
        {'cmd': 'cluster [--embeddings]', 'purpose': 'recurring task-type clusters (②repeat)', 'json': True},
        {'cmd': 'search <q> [--limit]', 'purpose': 'FTS5 lexical search over user prompts', 'json': True},
        {'cmd': 'pack <q> [--max-tokens]', 'purpose': 'token-budgeted cited excerpts for a topic', 'json': True},
        {'cmd': 'doctor', 'purpose': 'health: integrity, coverage, fts, safe-to-gc', 'json': True},
        {'cmd': 'stats', 'purpose': 'session counts by source/kind', 'json': True},
        {'cmd': 'capabilities', 'purpose': 'this self-description', 'json': True},
    ],
    'typical_flow': ['triage', 'ingest', 'digest', 'cluster'],
}


def cmd_triage(args):
    st = open_store()
    sv = st.stored_version()
    if sv is not None and sv != SCHEMA_VERSION:
        print(json.dumps({'status': 'schema-mismatch', 'db_version': sv, 'expected': SCHEMA_VERSION,
                          'next_command': 'mirror ingest --full'}, ensure_ascii=False))
        st.close(); sys.exit(VERSION_MISMATCH)
    n = st.session_count()
    if n == 0:
        print(json.dumps({'status': 'empty', 'next_command': 'mirror ingest'}, ensure_ascii=False))
        st.close(); sys.exit(EMPTY)
    stats = st.stats()
    fts = st.fts_status()
    last = st.last_ingest()
    st.close()
    print(json.dumps({'status': 'ok', 'sessions_by_source_kind': stats,
                      'real': {s: k.get('real', 0) for s, k in stats.items()},
                      'last_ingest': last,
                      'fts': fts, 'next_command': 'mirror digest --json && mirror cluster --json'},
                     ensure_ascii=False))


def cmd_doctor(args):
    st = open_store()
    integ = st.integrity_check()
    fts = st.fts_status()
    n_sess = st.session_count()
    orphan_msg = st.db.execute(
        "SELECT COUNT(*) FROM message m LEFT JOIN session s ON m.session_id=s.id "
        "WHERE s.id IS NULL").fetchone()[0]
    tracked = st.db.execute("SELECT COUNT(*) FROM ingest_state").fetchone()[0]
    wal = DB_PATH + '-wal'
    wal_kb = round(os.path.getsize(wal) / 1024, 1) if os.path.exists(wal) else 0
    digest_stale = not os.path.exists(DIGEST_PATH)
    st.close()
    healthy = (integ == 'ok' and orphan_msg == 0 and fts.get('in_sync', True))
    report = {'status': 'healthy' if healthy else 'degraded',
              'integrity': integ, 'sessions': n_sess, 'files_tracked': tracked,
              'orphan_messages': orphan_msg, 'fts': fts,
              'safe_to_gc': {'wal_kb': wal_kb, 'digest_missing': digest_stale},
              'advice': [] if healthy else
              (['run: mirror ingest --full (integrity)'] if integ != 'ok' else
               ['run any search to rebuild FTS'] if not fts.get('in_sync', True) else [])}
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    sys.exit(OK if healthy else HEALTH_FAIL)


def cmd_capabilities(args):
    print(json.dumps(CAPABILITIES, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(prog='mirror')
    sub = ap.add_subparsers(dest='cmd', required=True)
    pi = sub.add_parser('ingest', help='parse sources into SQLite (incremental)')
    pi.add_argument('--full', action='store_true', help='force full re-parse')
    pi.set_defaults(fn=cmd_ingest)
    ps = sub.add_parser('stats', help='session counts by source/kind')
    ps.set_defaults(fn=cmd_stats)
    pt = sub.add_parser('triage', help='safe first call: readiness + next_command')
    pt.set_defaults(fn=cmd_triage)
    pdo = sub.add_parser('doctor', help='health check: integrity/coverage/fts')
    pdo.add_argument('--pretty', action='store_true')
    pdo.set_defaults(fn=cmd_doctor)
    pcap = sub.add_parser('capabilities', help='self-describing API')
    pcap.set_defaults(fn=cmd_capabilities)
    pd = sub.add_parser('digest', help='rebuild digest.json from SQLite -> stdout')
    pd.add_argument('--pretty', action='store_true')
    pd.set_defaults(fn=cmd_digest)
    pr = sub.add_parser('report', help='render the audit report as a standalone HTML page')
    pr.add_argument('--embeddings', action='store_true')
    pr.add_argument('--no-open', action='store_true', help='do not auto-open the page (default: opens it)')
    pr.set_defaults(fn=cmd_report)
    pc = sub.add_parser('cluster', help='cluster task prompts into recurring task types')
    pc.add_argument('--embeddings', action='store_true', help='use local MiniLM (opt-in; else TF-IDF)')
    pc.add_argument('--threshold', type=float, default=None)
    pc.add_argument('--pretty', action='store_true')
    pc.set_defaults(fn=cmd_cluster)
    pse = sub.add_parser('search', help='FTS5 lexical search over user prompts')
    pse.add_argument('query')
    pse.add_argument('--limit', type=int, default=20)
    pse.set_defaults(fn=cmd_search)
    pp = sub.add_parser('pack', help='token-budgeted cited excerpts for a query')
    pp.add_argument('query')
    pp.add_argument('--max-tokens', type=int, default=4000, dest='max_tokens')
    pp.set_defaults(fn=cmd_pack)
    args = ap.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
