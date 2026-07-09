"""Optional local embeddings (ARCHITECTURE.md §6, opt-in). Zero-dep by default:
if fastembed isn't installed, embed() returns None and cluster.py degrades to TF-IDF.
Enable with:  pip install fastembed   (downloads a ~90MB ONNX model on first use)."""

_MODEL = None
_DEFAULT = 'intfloat/multilingual-e5-small'   # handles Chinese + English


def available():
    try:
        import fastembed  # noqa: F401
        return True
    except Exception:
        return False


def embed(texts, model_name=_DEFAULT):
    """Return list[list[float]] embeddings, or None if fastembed is unavailable."""
    try:
        from fastembed import TextEmbedding
    except Exception:
        return None
    global _MODEL
    if _MODEL is None:
        _MODEL = TextEmbedding(model_name)     # first call downloads the model (opt-in network)
    return [list(v) for v in _MODEL.embed(list(texts))]
