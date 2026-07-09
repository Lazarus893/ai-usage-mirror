"""②repeat-ask / task-type clustering (ARCHITECTURE.md §7).

Default: pure-Python TF-IDF + greedy leader clustering (zero-dep, CJK-aware via filters._WORD).
Opt-in: local MiniLM embeddings (embed.py) for paraphrase-level similarity; degrades to TF-IDF.
Emits candidate clusters; the Report-layer LLM names the task archetypes semantically.
"""
import math, collections
import filters as F


def _tokens(text):
    return [w.lower() for w in F._WORD.findall(text) if w.lower() not in F._STOP]


def _tfidf(token_lists):
    n = len(token_lists)
    df = collections.Counter()
    for toks in token_lists:
        df.update(set(toks))
    vecs = []
    for toks in token_lists:
        tf = collections.Counter(toks)
        v = {t: c * (math.log((n + 1) / (df[t] + 1)) + 1) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items()})
    return vecs


def _cos_sparse(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def _cos_dense(a, b):
    return sum(x * y for x, y in zip(a, b))


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _leader_cluster(order, vecs, cos, threshold):
    clusters = []                       # each: {'leader': idx, 'members': [idx, ...]}
    for i in order:
        best, best_sim = None, threshold
        for c in clusters:
            s = cos(vecs[i], vecs[c['leader']])
            if s >= best_sim:
                best, best_sim = c, s
        if best is None:
            clusters.append({'leader': i, 'members': [i]})
        else:
            best['members'].append(i)
    return clusters


def cluster_tasks(db, use_embeddings=False, threshold=None, limit=500):
    rows = db.execute(
        "SELECT m.text, s.id, s.source FROM message m JOIN session s ON m.session_id=s.id "
        "WHERE s.kind='real' AND m.is_task=1 AND m.text IS NOT NULL LIMIT ?", (limit,)).fetchall()
    texts = [r[0] for r in rows]
    sess = [r[1] for r in rows]
    srcs = [r[2] for r in rows]
    if not texts:
        return {'mode': 'none', 'n_tasks': 0, 'clusters': []}

    mode = 'lexical-tfidf'
    vecs = cos = None
    if use_embeddings:
        import embed
        ev = embed.embed(texts)
        if ev:
            vecs = [_unit(v) for v in ev]
            cos = _cos_dense
            mode = 'embeddings'
            if threshold is None:
                threshold = 0.82
    if vecs is None:
        vecs = _tfidf([_tokens(t) for t in texts])
        cos = _cos_sparse
        if threshold is None:
            threshold = 0.28

    order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))   # rich prompts as leaders
    clusters = _leader_cluster(order, vecs, cos, threshold)

    recurring = []
    singleton_samples = []
    for c in clusters:
        mem = c['members']
        if len(mem) < 2:
            if len(singleton_samples) < 12:
                singleton_samples.append(texts[c['leader']][:120])
            continue
        terms = collections.Counter()
        for i in mem:
            terms.update(set(_tokens(texts[i])))
        recurring.append({
            'size': len(mem),
            'representative': texts[c['leader']][:140],
            'top_terms': [t for t, _ in terms.most_common(6)],
            'sources': dict(collections.Counter(srcs[i] for i in mem)),
            'sample_sessions': [sess[i] for i in mem[:3]],
        })
    recurring.sort(key=lambda x: -x['size'])
    n_singletons = sum(1 for c in clusters if len(c['members']) == 1)
    return {'mode': mode, 'threshold': threshold, 'n_tasks': len(texts),
            'n_singletons': n_singletons, 'clusters': recurring, 'singleton_samples': singleton_samples}
