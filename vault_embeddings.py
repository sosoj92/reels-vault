#!/usr/bin/env python3
"""
vault_embeddings.py - Index semantique local des sources (V2).

Complement de la recherche lexicale (jamais un remplacement) :
- modele local multilingual-e5-small (ONNX quantifie, ~118 Mo, CPU, sans torch) ;
- vecteurs stockes dans vault.db (table intelligence_embeddings), par chunk ;
- index incremental : une source n'est revectorisee que si son texte change ;
- reconstruction possible (--rebuild) ;
- si le modele ou onnxruntime manquent : EmbeddingUnavailable, et la recherche
  retombe sur le lexical.

Calibrage (banc d'essai du 2026-09-28, 64 sources reelles, 15 paraphrases,
12 questions hors Vault) : les cosinus e5 sont comprimes (0,80-0,88) et un
seuil absolu ferait remonter du bruit. On utilise donc un z-score par rapport
a la distribution des sources. z >= 2,4 : 0 faux positif sur 12, 5 vraies
paraphrases gardees sur 10. Un resultat purement semantique n'est admis qu'au
dela de ce seuil, et presente comme "a verifier".
"""

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = Path(
    os.environ.get("VAULT_EMBEDDING_MODEL_DIR") or ROOT / "models" / "multilingual-e5-small"
)
MODEL_ID = "multilingual-e5-small-q8"
CHUNK_CHARS = 700
MAX_TRANSCRIPT_CHUNKS = 3
HEAD_CHARS = 1500
# Une source trouvee UNIQUEMENT par le sens doit se detacher nettement.
# Plancher fixe, releve automatiquement par le calibrage de chaque index.
SEMANTIC_ONLY_MIN_Z = float(os.environ.get("VAULT_SEMANTIC_MIN_Z", "2.4"))
CALIBRATION_MARGIN_Z = 0.15
# Questions sans rapport avec un Vault de veille : leur meilleur z mesure le
# "bruit" du corpus. Le seuil semantique pur est place au-dessus.
CALIBRATION_PROBES = (
    "météo de la semaine prochaine", "recette de gâteau au chocolat",
    "entraînement pour courir un marathon", "horaires d'ouverture de la piscine",
    "planter un potager au printemps", "réparer la chaîne de mon vélo",
    "préparer un voyage au Japon", "concert de jazz ce week-end", "éduquer un chiot",
    "apprendre le tricot", "résultat du match de tennis", "film d'horreur à voir",
    "pêche à la truite en rivière", "coupe de cheveux tendance", "organiser un mariage",
    "déménager sans stress", "observer les étoiles", "jeux de société en famille",
    "tutoriel de maquillage", "déboucher un évier",
)
# En dessous, le z-score n'a pas de sens statistique : pas de resultat
# purement semantique (le semantique ne sert alors qu'a reordonner).
MIN_CORPUS_FOR_SEMANTIC_ONLY = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_embeddings (
    source_id   INTEGER NOT NULL REFERENCES intelligence_sources(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    chunk_text  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, chunk_index, model)
);

CREATE TABLE IF NOT EXISTS intelligence_embedding_calibration (
    model       TEXT PRIMARY KEY,
    threshold_z REAL NOT NULL,
    probe_max_z REAL NOT NULL,
    corpus_size INTEGER NOT NULL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intelligence_embedding_state (
    source_id   INTEGER NOT NULL REFERENCES intelligence_sources(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    doc_hash    TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    indexed_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, model)
);
"""


class EmbeddingUnavailable(RuntimeError):
    pass


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class OnnxE5Backend:
    """Encodeur e5 local. Chargement paresseux (~1,6 s), puis ~7 ms/requete."""

    name = "onnx-e5"

    def __init__(self, model_dir=None, threads=2):
        self.model_dir = Path(model_dir or DEFAULT_MODEL_DIR)
        self.model = MODEL_ID
        self.threads = threads
        self._session = None
        self._tokenizer = None
        self._inputs = None

    def available(self):
        return (
            (self.model_dir / "onnx" / "model_quantized.onnx").exists()
            and (self.model_dir / "tokenizer.json").exists()
        )

    def _load(self):
        if self._session is not None:
            return
        if not self.available():
            raise EmbeddingUnavailable(f"modele d'embedding absent : {self.model_dir}")
        try:
            import numpy  # noqa: F401
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise EmbeddingUnavailable(f"dependance manquante : {exc}") from exc
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        self._session = ort.InferenceSession(
            str(self.model_dir / "onnx" / "model_quantized.onnx"),
            options,
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(512)
        self._inputs = {item.name for item in self._session.get_inputs()}

    def _encode(self, text):
        import numpy as np

        self._load()
        encoding = self._tokenizer.encode(text)
        ids = np.array([encoding.ids], dtype=np.int64)
        mask = np.array([encoding.attention_mask], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feed)[0]
        pooled = (hidden * mask[..., None]).sum(1) / mask.sum(1, keepdims=True)
        vector = pooled[0]
        return (vector / np.linalg.norm(vector)).astype("float32")

    def embed_query(self, text):
        return self._encode("query: " + str(text or ""))

    def embed_passage(self, text):
        return self._encode("passage: " + str(text or ""))


_DEFAULT_BACKEND = None


def default_backend():
    """Backend partage (un seul chargement par processus), ou None."""
    global _DEFAULT_BACKEND
    if os.environ.get("VAULT_SEMANTIC", "").strip().lower() in ("0", "off", "false", "non"):
        return None
    if _DEFAULT_BACKEND is None:
        backend = OnnxE5Backend()
        if not backend.available():
            return None
        _DEFAULT_BACKEND = backend
    return _DEFAULT_BACKEND


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    return conn


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def source_chunks(doc_fields):
    """
    Decoupe une source : un chunk "tete" (titre, taxonomie, ressources,
    descriptions) puis quelques chunks de transcript prefixes du titre.
    """
    title = (doc_fields.get("title") or "").strip()
    head = ". ".join(
        part for part in (
            title,
            doc_fields.get("taxonomy") or "",
            doc_fields.get("resources") or "",
            doc_fields.get("description") or "",
        )
        if part and part.strip()
    )
    chunks = [head[:HEAD_CHARS]] if head.strip() else []
    transcript = " ".join(str(doc_fields.get("transcript") or "").split())
    for start in range(0, min(len(transcript), CHUNK_CHARS * MAX_TRANSCRIPT_CHUNKS), CHUNK_CHARS):
        piece = transcript[start:start + CHUNK_CHARS].strip()
        if piece:
            chunks.append(f"{title}. {piece}" if title else piece)
    return chunks


def _documents(conn):
    # Reutilise exactement le chargement de la recherche lexicale.
    import vault_intelligence_context as context_layer

    documents = []
    for doc in context_layer._load_documents(conn):
        documents.append((int(doc["source_id"]), context_layer._doc_fields(doc)))
    return documents


def _hash(chunks):
    raw = json.dumps([MODEL_ID, chunks], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def index_sources(conn, backend=None, *, rebuild=False, source_ids=None):
    """
    Index incremental. Retourne des statistiques ; n'ecrit que la table
    d'embeddings (donnee derivee, jamais une donnee utilisateur).
    """
    import numpy as np

    backend = backend or default_backend()
    if backend is None:
        raise EmbeddingUnavailable("aucun backend d'embedding disponible")
    ensure_schema(conn)
    started = time.perf_counter()
    stats = {"model": backend.model, "indexed": 0, "unchanged": 0, "removed": 0, "chunks": 0}

    if rebuild:
        conn.execute("DELETE FROM intelligence_embeddings WHERE model = ?", (backend.model,))
        conn.execute("DELETE FROM intelligence_embedding_state WHERE model = ?", (backend.model,))

    state = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT source_id, doc_hash FROM intelligence_embedding_state WHERE model = ?",
            (backend.model,),
        )
    }
    documents = _documents(conn)
    present = {source_id for source_id, _ in documents}
    for stale in set(state) - present:
        conn.execute(
            "DELETE FROM intelligence_embeddings WHERE source_id = ? AND model = ?",
            (stale, backend.model),
        )
        conn.execute(
            "DELETE FROM intelligence_embedding_state WHERE source_id = ? AND model = ?",
            (stale, backend.model),
        )
        stats["removed"] += 1

    for source_id, fields in documents:
        if source_ids is not None and source_id not in source_ids:
            continue
        chunks = source_chunks(fields)
        if not chunks:
            continue
        doc_hash = _hash(chunks)
        if state.get(source_id) == doc_hash:
            stats["unchanged"] += 1
            continue
        stamp = _now()
        conn.execute(
            "DELETE FROM intelligence_embeddings WHERE source_id = ? AND model = ?",
            (source_id, backend.model),
        )
        for index, chunk in enumerate(chunks):
            vector = np.asarray(backend.embed_passage(chunk), dtype="float32")
            conn.execute(
                """
                INSERT INTO intelligence_embeddings(
                    source_id, chunk_index, model, dim, vector, chunk_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source_id, index, backend.model, int(vector.shape[0]),
                 vector.tobytes(), chunk[:2000], stamp),
            )
        conn.execute(
            """
            INSERT INTO intelligence_embedding_state(source_id, model, doc_hash, chunk_count, indexed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id, model) DO UPDATE SET
                doc_hash = excluded.doc_hash,
                chunk_count = excluded.chunk_count,
                indexed_at = excluded.indexed_at
            """,
            (source_id, backend.model, doc_hash, len(chunks), stamp),
        )
        stats["indexed"] += 1
        stats["chunks"] += len(chunks)
        # Commit par source : un index partiel reste coherent si on interrompt.
        conn.commit()
    conn.commit()
    has_calibration = conn.execute(
        "SELECT 1 FROM intelligence_embedding_calibration WHERE model = ?", (backend.model,)
    ).fetchone()
    if stats["indexed"] or stats["removed"] or rebuild or not has_calibration:
        stats["calibration"] = calibrate(conn, backend)
    stats["elapsed_s"] = round(time.perf_counter() - started, 2)
    return stats


def calibrate(conn, backend):
    """Seuil du semantique pur = max(plancher, pire sonde hors sujet + marge)."""
    rows = load_index(conn, backend.model)
    if not rows:
        return None
    probe_max = max(
        max(item["z"] for item in score_rows(rows, backend.embed_query(probe))["scores"].values())
        for probe in CALIBRATION_PROBES
    )
    corpus = len({row[0] for row in rows})
    threshold = max(SEMANTIC_ONLY_MIN_Z, probe_max + CALIBRATION_MARGIN_Z)
    conn.execute(
        """
        INSERT INTO intelligence_embedding_calibration(
            model, threshold_z, probe_max_z, corpus_size, computed_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(model) DO UPDATE SET
            threshold_z = excluded.threshold_z, probe_max_z = excluded.probe_max_z,
            corpus_size = excluded.corpus_size, computed_at = excluded.computed_at
        """,
        (backend.model, round(threshold, 3), round(probe_max, 3), corpus, _now()),
    )
    conn.commit()
    return {"threshold_z": round(threshold, 3), "probe_max_z": round(probe_max, 3),
            "corpus_size": corpus}


def semantic_threshold(conn, model=MODEL_ID):
    """Seuil calibre de l'index (plancher si le calibrage est absent)."""
    if not _table_exists(conn, "intelligence_embedding_calibration"):
        return SEMANTIC_ONLY_MIN_Z
    row = conn.execute(
        "SELECT threshold_z FROM intelligence_embedding_calibration WHERE model = ?", (model,)
    ).fetchone()
    return max(SEMANTIC_ONLY_MIN_Z, float(row[0])) if row else SEMANTIC_ONLY_MIN_Z


def index_status(conn, model=MODEL_ID):
    if not _table_exists(conn, "intelligence_embedding_state"):
        return {"model": model, "indexed_sources": 0, "chunks": 0}
    sources = conn.execute(
        "SELECT COUNT(*) FROM intelligence_embedding_state WHERE model = ?", (model,)
    ).fetchone()[0]
    chunks = conn.execute(
        "SELECT COUNT(*) FROM intelligence_embeddings WHERE model = ?", (model,)
    ).fetchone()[0]
    return {"model": model, "indexed_sources": sources, "chunks": chunks,
            "semantic_only_min_z": semantic_threshold(conn, model)}


def load_index(conn, model=MODEL_ID):
    """
    Lit l'index (dans le thread de la connexion : SQLite interdit le partage
    d'une connexion entre threads). Retourne [] si l'index est absent.
    """
    if not _table_exists(conn, "intelligence_embeddings"):
        return []
    return [
        tuple(row) for row in conn.execute(
            "SELECT source_id, chunk_index, dim, vector, chunk_text "
            "FROM intelligence_embeddings WHERE model = ?",
            (model,),
        ).fetchall()
    ]


def semantic_scores(conn, query, backend=None):
    """
    Scores semantiques par source : {source_id: {cos, z, chunk_index, chunk_text}}.
    Retourne None si l'index est vide ou si le backend est indisponible :
    l'appelant retombe alors sur le lexical.
    """
    backend = backend or default_backend()
    rows = load_index(conn, getattr(backend, "model", MODEL_ID))
    if not rows or backend is None:
        return None
    return score_rows(rows, backend.embed_query(query))


def score_rows(rows, query_vector):
    """Calcul pur (sans base) : utilisable dans un thread avec echeance."""
    import numpy as np

    query_vector = np.asarray(query_vector, dtype="float32")
    best = {}
    for source_id, chunk_index, dim, blob, chunk_text in rows:
        vector = np.frombuffer(blob, dtype="float32")
        if vector.shape[0] != query_vector.shape[0]:
            continue
        cos = float(vector @ query_vector)
        if source_id not in best or cos > best[source_id]["cos"]:
            best[source_id] = {"cos": cos, "chunk_index": chunk_index, "chunk_text": chunk_text}
    if not best:
        return None
    values = np.array([item["cos"] for item in best.values()])
    mean, std = float(values.mean()), float(values.std()) or 1e-6
    for item in best.values():
        item["z"] = (item["cos"] - mean) / std
    return {"corpus_size": len(best), "scores": best}


def main():
    parser = argparse.ArgumentParser(description="Index semantique local des sources")
    parser.add_argument("--db", default=str(ROOT / "vault.db"))
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if args.status:
            result = index_status(conn)
        else:
            result = index_sources(conn, rebuild=args.rebuild)
    except EmbeddingUnavailable as exc:
        result = {"status": "unavailable", "reason": str(exc)}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
