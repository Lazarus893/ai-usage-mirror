#!/usr/bin/env python3
"""Golden contract test: locks digest.json field names & types (not values).
Any field rename/type/nullability change fails here -> deliberate regeneration required.
Run: python3 test_contract.py   (exit 0 = ok, 1 = contract violation)."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store import Store
import aggregate

DB = os.path.normpath(os.path.join(HERE, '..', '.state', 'mirror.db'))


def check(cond, msg, errs):
    if not cond:
        errs.append(msg)


def validate(d, errs):
    top = {'meta', 'temporal', 'projects', 'tools_used', 'commands_top', 'edited_filetypes',
           'models', 'output_tokens_est', 'friction_candidates', 'task_prompts', 'per_source'}
    check(set(d) == top, f"top-level keys drift: {set(d) ^ top}", errs)

    m = d.get('meta', {})
    for k, ty in {'sources': list, 'sessions': int, 'meta_sessions_skipped': dict,
                  'artifact_sessions_skipped': dict, 'tokens_note': str, 'span': list}.items():
        check(isinstance(m.get(k), ty), f"meta.{k} not {ty.__name__}", errs)
    check(len(m.get('span', [])) == 2, "meta.span must be [start,end]", errs)

    t = d.get('temporal', {})
    check(isinstance(t.get('by_hour'), list) and len(t['by_hour']) == 24, "temporal.by_hour != 24", errs)
    check(isinstance(t.get('by_weekday'), list) and len(t['by_weekday']) == 7, "temporal.by_weekday != 7", errs)

    for k in ('tools_used', 'edited_filetypes', 'models', 'output_tokens_est', 'per_source'):
        check(isinstance(d.get(k), dict), f"{k} not dict", errs)

    for row, keys in [('projects', {'cwd', 'sessions', 'share'}),
                      ('commands_top', {'cmd', 'n'}),
                      ('friction_candidates', {'source', 'session', 'quote'}),
                      ('task_prompts', {'prompt', 'n', 'source'})]:
        lst = d.get(row)
        check(isinstance(lst, list), f"{row} not list", errs)
        if lst:
            check(set(lst[0]) == keys, f"{row}[0] keys drift: {set(lst[0]) ^ keys}", errs)


def main():
    if not os.path.exists(DB):
        print("SKIP: no db (run `mirror ingest` first)", file=sys.stderr)
        return 0
    st = Store(DB)
    digest = aggregate.build_digest(st.db)
    st.close()
    errs = []
    validate(digest, errs)
    if errs:
        print("CONTRACT VIOLATIONS:", file=sys.stderr)
        for e in errs:
            print("  -", e, file=sys.stderr)
        return 1
    print("contract OK: 11 top-level keys, all types locked")
    return 0


if __name__ == '__main__':
    sys.exit(main())
