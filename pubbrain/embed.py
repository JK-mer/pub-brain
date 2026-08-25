"""Local embeddings via ollama, and brute-force cosine search over them (#17).

No API and no cost: the model runs on this machine. At ~7,000 vectors a full
scan is microseconds, so there is no index — adding one would be complexity
bought with nothing.

Vectors are stored L2-normalised, which makes cosine similarity a dot product.
"""

import numpy as np
import requests

OLLAMA_URL = "http://localhost:11434/api/embed"
MODEL = "embeddinggemma"
DIM = 768

# embeddinggemma is trained with asymmetric prefixes: documents and queries are
# encoded differently, and using the wrong one measurably degrades retrieval.
DOC_TEMPLATE = "title: {title} | text: {text}"
QUERY_TEMPLATE = "task: search result | query: {query}"

# Its context is 2k tokens. Sections average ~330 words, but Reports carry some
# long ones and a silent truncation deep in a batch is hard to notice.
MAX_WORDS = 1000


class OllamaUnreachable(RuntimeError):
    pass


class ShortResponse(RuntimeError):
    """Fewer vectors came back than were asked for — the one failure that would
    otherwise corrupt the index silently."""


def _post(inputs, model, timeout):
    try:
        r = requests.post(OLLAMA_URL, json={"model": model, "input": inputs},
                          timeout=timeout)
    except requests.RequestException as exc:
        raise OllamaUnreachable(f"ollama not reachable at {OLLAMA_URL}: {exc}") from exc
    r.raise_for_status()
    vectors = r.json()["embeddings"]
    # Callers zip these against their input rows. A short response would pair
    # vectors to the wrong sections and every affected row would then retrieve
    # as a different document, with nothing anywhere to reveal it.
    if len(vectors) != len(inputs):
        raise ShortResponse(
            f"asked for {len(inputs)} embeddings, got {len(vectors)}")
    return vectors


def embed_documents(texts, titles=None, model=MODEL, timeout=300):
    """Embed passages for storage. `titles` gives each its heading, which the
    model is trained to use as context."""
    titles = titles or [None] * len(texts)
    prepared = [
        DOC_TEMPLATE.format(title=t or "none",
                            text=" ".join((x or "").split()[:MAX_WORDS]))
        for x, t in zip(texts, titles)
    ]
    return normalise(np.array(_post(prepared, model, timeout), dtype=np.float32))


def embed_query(query, model=MODEL, timeout=60):
    """Embed a search string. Uses the query prefix, not the document one."""
    vec = _post([QUERY_TEMPLATE.format(query=query)], model, timeout)[0]
    return normalise(np.array([vec], dtype=np.float32))[0]


def normalise(matrix):
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


def pack(vector) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def unpack(blob, dim=DIM):
    return np.frombuffer(blob, dtype=np.float32).reshape(dim)


FUSION_K = 60


def fuse(rankings, k=FUSION_K):
    """Reciprocal rank fusion: merge ranked id lists by position, never by score.

    bm25 is unbounded and lower-is-better, cosine is 0-1 and higher-is-better,
    and neither is calibrated against the other — so any weighted sum of the two
    is arbitrary. RRF only reads the ordering, which is the part both agree on.
    `k` damps the head so one ranker cannot dominate on its top hit alone.
    """
    scores = {}
    for ranking in rankings:
        for position, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def rank(query_vector, matrix, limit=10):
    """Indices and scores of the closest rows, best first. Vectors are
    normalised, so the dot product is the cosine."""
    if matrix.size == 0:
        return []
    scores = matrix @ query_vector
    top = np.argsort(-scores)[:limit]
    return [(int(i), float(scores[i])) for i in top]
