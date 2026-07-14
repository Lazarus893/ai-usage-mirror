"""Codex parser: ~/.codex/sessions/**/*.jsonl (rollout files)."""
import os, json, collections
from redact import redact
import filters as F

GLOB = "~/.codex/sessions/**/*.jsonl"


def parse(path):
    cwd = None
    sid = None
    model = None
    originator = None
    hints = collections.Counter()
    n_user = n_asst = 0
    sess_out = 0
    first_prompt = task_prompt = None
    seq = 0
    last_asst_seq = 0
    prev_asst = False
    messages = []
    tool_calls = []
    started = ended = None

    with open(path, errors='ignore') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get('type')
            p = rec.get('payload') or {}
            ts = rec.get('timestamp')
            dt = F.parse_ts(ts)
            if dt:
                if started is None:
                    started = ts
                ended = ts

            if t == 'session_meta':
                cwd = p.get('cwd') or cwd
                sid = p.get('id') or sid
                originator = p.get('originator') or originator
            elif t == 'turn_context':
                if p.get('cwd'):
                    cwd = p['cwd']
                if p.get('model'):
                    model = p['model']
            elif t == 'event_msg':
                pt = p.get('type')
                if pt == 'user_message':
                    text = F.unwrap(p.get('message') or '')
                    if F.is_injected(text) or not F.is_real_prompt(text):
                        prev_asst = False
                        continue
                    seq += 1
                    n_user += 1
                    if first_prompt is None:
                        first_prompt = text
                    task = F.is_task_prompt(text)
                    if task and task_prompt is None:
                        task_prompt = text
                    messages.append({
                        'seq': seq, 'ts': ts, 'role': 'user',
                        'text': redact(text)[:4000],
                        'is_task': 1 if task else 0,
                        'is_friction': 1 if F.is_friction(text, prev_asst) else 0,
                        'sig': ' '.join(F.signature(text)) if task else None,
                    })
                    prev_asst = False
                elif pt == 'agent_message':
                    seq += 1
                    last_asst_seq = seq
                    n_asst += 1
                    prev_asst = True
                elif pt == 'token_count':
                    info = p.get('info') or {}
                    tot = info.get('total_token_usage') or {}   # CUMULATIVE -> max
                    if isinstance(tot, dict):
                        v = (tot.get('output_tokens', 0) or 0) + (tot.get('reasoning_output_tokens', 0) or 0)
                        sess_out = max(sess_out, v)
            elif t == 'response_item' and p.get('type') == 'function_call':
                name = p.get('name', '?')
                cmd_key = workdir = pkgs = None
                args = p.get('arguments')
                if isinstance(args, str):
                    try:
                        aj = json.loads(args)
                    except Exception:
                        aj = None
                    if isinstance(aj, dict):
                        wp = aj.get('workdir') or aj.get('cwd')
                        if wp:
                            pr = F.project_root(wp)
                            if pr:
                                hints[pr] += 1
                            workdir = redact(wp)
                        cmd = aj.get('cmd') or aj.get('command')
                        if isinstance(cmd, list):
                            cmd = ' '.join(str(x) for x in cmd)
                        if isinstance(cmd, str):
                            for tgt in F.cd_targets(cmd):
                                pr = F.project_root(tgt)
                                if pr:
                                    hints[pr] += 1
                            cmd_key = F.command_key(cmd)
                            pkgs = ','.join(F.install_pkgs(cmd)) or None
                tool_calls.append({'after_seq': last_asst_seq, 'tool': 'codex:' + name,
                                   'cmd_key': cmd_key, 'file_ext': None, 'workdir': workdir, 'pkgs': pkgs})

    if n_user == 0 and n_asst == 0:
        return None
    kind = 'meta' if n_user == 0 else ('artifact' if task_prompt is None else 'real')
    return {
        'id': 'cx:' + (sid or os.path.splitext(os.path.basename(path))[0]), 'source': 'codex',
        'cwd_raw': redact(cwd or ''), 'project': F.resolve_project(cwd, hints),
        'model': model, 'originator': originator,
        'started_at': started, 'ended_at': ended,
        'n_user': n_user, 'n_asst': n_asst, 'out_tokens': sess_out,
        'kind': kind, 'messages': messages, 'tool_calls': tool_calls,
    }
