"""Claude Code parser: ~/.claude/projects/**/*.jsonl (one file = one session)."""
import os, json, collections
from redact import redact
import filters as F

GLOB = "~/.claude/projects/**/*.jsonl"


def parse(path):
    """Return a normalized session dict, or None if the file has no messages at all."""
    cwd = None
    base = os.path.basename(path)
    sid = os.path.splitext(base)[0]          # full uuid / agent-hash — unique (no truncation!)
    is_sidechain = base.startswith('agent-')  # sub-agent transcript, not a direct user session
    model = None
    hints = collections.Counter()
    n_user = n_asst = 0
    out_tokens = 0
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
            if rec.get('cwd'):
                cwd = rec['cwd']
            ts = rec.get('timestamp')
            dt = F.parse_ts(ts)
            if dt:
                if started is None:
                    started = ts
                ended = ts

            if t == 'user':
                c = (rec.get('message') or {}).get('content')
                if isinstance(c, str):
                    raw = c
                elif isinstance(c, list):
                    raw = '\n'.join(b.get('text', '') for b in c
                                    if isinstance(b, dict) and b.get('type') == 'text')
                else:
                    raw = ''
                text = F.unwrap(raw)
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

            elif t == 'assistant':
                msg = rec.get('message') or {}
                m = msg.get('model')
                if m:
                    model = m
                u = msg.get('usage') or {}
                if isinstance(u, dict):
                    out_tokens += u.get('output_tokens', 0) or 0
                seq += 1
                last_asst_seq = seq
                for b in (msg.get('content') or []):
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        name = b.get('name', '?')
                        inp = b.get('input') or {}
                        cmd_key = file_ext = workdir = pkgs = None
                        if isinstance(inp, dict):
                            if name == 'Bash' and inp.get('command'):
                                cmd = inp['command']
                                for tgt in F.cd_targets(cmd):
                                    pr = F.project_root(tgt)
                                    if pr:
                                        hints[pr] += 1
                                cmd_key = F.command_key(cmd)
                                pkgs = ','.join(F.install_pkgs(cmd)) or None
                            if name in ('Edit', 'Write', 'NotebookEdit') and inp.get('file_path'):
                                fp = inp['file_path']
                                file_ext = os.path.splitext(fp)[1].lower() or None
                                pr = F.project_root(fp)
                                if pr:
                                    hints[pr] += 1
                        tool_calls.append({'after_seq': last_asst_seq, 'tool': name, 'cmd_key': cmd_key,
                                           'file_ext': file_ext, 'workdir': workdir, 'pkgs': pkgs})
                n_asst += 1
                prev_asst = True

    if n_user == 0 and n_asst == 0:
        return None
    if is_sidechain or n_user == 0:
        kind = 'meta'                                          # sub-agent sidechain or automated
    elif task_prompt is None:
        kind = 'artifact'                                      # trivial input only
    else:
        kind = 'real'
    return {
        'id': 'cc:' + sid, 'source': 'claude-code',
        'cwd_raw': redact(cwd or ''), 'project': F.resolve_project(cwd, hints),
        'model': model, 'originator': None,
        'started_at': started, 'ended_at': ended,
        'n_user': n_user, 'n_asst': n_asst, 'out_tokens': out_tokens,
        'kind': kind, 'messages': messages, 'tool_calls': tool_calls,
    }
