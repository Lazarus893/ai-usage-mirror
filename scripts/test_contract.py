#!/usr/bin/env python3
"""Golden contract test: locks digest.json field names & types (not values).
Any field rename/type/nullability change fails here -> deliberate regeneration required.
Run: python3 test_contract.py   (exit 0 = ok, 1 = contract violation)."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store import Store
import aggregate
import profile as profile_mod

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


def validate_profile(p, errs):
    top = {'prompting_style', 'stack_fingerprint', 'verification_habits', 'friction_themes'}
    check(set(p) == top, f"profile top-level keys drift: {set(p) ^ top}", errs)

    ps = p.get('prompting_style', {})
    for k, ty in {'n_prompts': int, 'median_chars': (int, float), 'length_mix': dict,
                  'cjk_share': (int, float), 'acceptance_rate': (int, float),
                  'discuss_first_rate': (int, float)}.items():
        check(isinstance(ps.get(k), ty), f"prompting_style.{k} bad type", errs)

    sf = p.get('stack_fingerprint', {})
    check(set(sf) == {'packages_installed', 'libs_mentioned', 'package_managers', 'languages'},
          "stack_fingerprint keys drift", errs)
    for row in ('packages_installed', 'libs_mentioned'):
        lst = sf.get(row)
        check(isinstance(lst, list), f"{row} not list", errs)
        if lst:
            check(set(lst[0]) == {'name', 'n'}, f"{row}[0] keys drift", errs)

    vh = p.get('verification_habits', {})
    check(set(vh) == {'buckets', 'commits', 'edits', 'test_to_edit_ratio'},
          "verification_habits keys drift", errs)

    ftl = p.get('friction_themes')
    check(isinstance(ftl, list), "friction_themes not list", errs)
    if ftl:
        check(set(ftl[0]) == {'theme', 'n', 'quotes'}, "friction_themes[0] keys drift", errs)


def main():
    if not os.path.exists(DB):
        print("SKIP: no db (run `mirror ingest` first)", file=sys.stderr)
        return 0
    st = Store(DB)
    digest = aggregate.build_digest(st.db)
    prof = profile_mod.build_profile(st.db)
    st.close()
    errs = []
    validate(digest, errs)
    validate_profile(prof, errs)
    if errs:
        print("CONTRACT VIOLATIONS:", file=sys.stderr)
        for e in errs:
            print("  -", e, file=sys.stderr)
        return 1
    print("contract OK: digest (11 keys) + profile (4 groups), all types locked")
    return 0


if __name__ == '__main__':
    sys.exit(main())
