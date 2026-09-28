#!/usr/bin/env python3
"""
vault_intelligence_context.py - Paquet de contexte fiable pour l'Assistant IA.

Cette couche est strictement en lecture. Elle transforme la memoire V3 en un
contexte borne et auditable, sans appel LLM ni embeddings.

Principes :
- SOURCE-FIRST : pour une question de rappel personnel, la recherche directe
  dans les sources sauvegardees (titre, descriptions, tags, ressources,
  transcript...) est le moteur principal (search_saved_sources) ;
- les entites, alias, summaries, claims et relations enrichissent ensuite ;
- robuste aux connexions sqlite3.Row comme aux tuples, et en lecture seule ;
- privilegier la pertinence sans masquer le statut epistemique ;
- ne jamais promouvoir un claim asserted en verified ;
- ne jamais exposer raw_result comme connaissance active ;
- renvoyer un paquet compact pour limiter le cout des assistants en aval.
"""

import json
import math
import re
import sqlite3
import unicodedata

import vault_intelligence as intelligence
import vault_intelligence_query as query_layer


class IntelligenceContextError(ValueError):
    pass


_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "dans", "avec",
    "pour", "les", "des", "une", "sur", "qui", "que", "est", "sont", "aux",
    "par", "plus", "comme", "comment", "quoi", "quel", "quelle", "quels",
    "quelles", "mon", "mes", "ton", "tes", "son", "ses",
}


def _fold(value):
    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(
        char for char in value
        if unicodedata.category(char) != "Mn"
    ).lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _query_parts(value):
    text = (value or "").strip()
    if not text:
        raise IntelligenceContextError("q is required")
    if len(text) > 500:
        raise IntelligenceContextError("q must be 500 characters or fewer")

    folded = _fold(text)
    tokens = [
        token for token in folded.split()
        if len(token) >= 2 and token not in _STOPWORDS
    ]
    # Conserver l'ordre, retirer les doublons.
    seen = set()
    tokens = [
        token for token in tokens
        if not (token in seen or seen.add(token))
    ]
    return folded, tokens


def _contains(text, needle):
    return bool(needle and needle in _fold(text))


def _token_hits(text, tokens):
    folded = _fold(text)
    return sum(1 for token in tokens if token in folded)


def _entity_search_data(conn):
    entities = {
        row["id"]: {
            "row": dict(row),
            "aliases": [],
            "claims": [],
            "sources": [],
            "relations": [],
        }
        for row in conn.execute(
            "SELECT * FROM intelligence_entities ORDER BY id"
        ).fetchall()
    }

    for row in conn.execute(
        "SELECT entity_id, alias FROM intelligence_entity_aliases"
    ).fetchall():
        if row["entity_id"] in entities:
            entities[row["entity_id"]]["aliases"].append(row["alias"])

    for row in conn.execute(
        """
        SELECT
            c.entity_id,
            c.claim_text,
            c.verification_status,
            c.source_id,
            s.source_url,
            s.title AS source_title
          FROM intelligence_claims c
          JOIN intelligence_sources s ON s.id = c.source_id
         WHERE c.entity_id IS NOT NULL
        """
    ).fetchall():
        if row["entity_id"] in entities:
            entities[row["entity_id"]]["claims"].append(dict(row))

    for row in conn.execute(
        """
        SELECT
            es.entity_id,
            s.id,
            s.source_url,
            s.title,
            s.author,
            s.platform,
            es.role
          FROM intelligence_entity_sources es
          JOIN intelligence_sources s ON s.id = es.source_id
        """
    ).fetchall():
        if row["entity_id"] in entities:
            entities[row["entity_id"]]["sources"].append(dict(row))

    for row in conn.execute(
        """
        SELECT
            r.subject_entity_id,
            r.object_entity_id,
            r.predicate,
            r.source_id,
            s.source_url,
            subject.name AS subject_name,
            object.name AS object_name
          FROM intelligence_relations r
          JOIN intelligence_entities subject
            ON subject.id = r.subject_entity_id
          JOIN intelligence_entities object
            ON object.id = r.object_entity_id
          LEFT JOIN intelligence_sources s
            ON s.id = r.source_id
        """
    ).fetchall():
        item = dict(row)
        if row["subject_entity_id"] in entities:
            entities[row["subject_entity_id"]]["relations"].append(item)
        if row["object_entity_id"] in entities:
            entities[row["object_entity_id"]]["relations"].append(item)

    return entities


def _score_entity(data, phrase, tokens):
    row = data["row"]
    score = 0
    matched_on = set()

    name = _fold(row.get("name"))
    canonical = _fold(row.get("canonical_key"))
    summary = _fold(row.get("summary"))

    if phrase and name == phrase:
        score += 140
        matched_on.add("name")
    elif phrase and phrase in name:
        score += 95
        matched_on.add("name")

    for alias in data["aliases"]:
        folded = _fold(alias)
        if phrase and folded == phrase:
            score += 125
            matched_on.add("alias")
        elif phrase and phrase in folded:
            score += 85
            matched_on.add("alias")

    if phrase and phrase in canonical:
        score += 55
        matched_on.add("canonical_key")
    if phrase and phrase in summary:
        score += 50
        matched_on.add("summary")

    token_hits = _token_hits(row.get("name"), tokens)
    if token_hits:
        score += token_hits * 22
        matched_on.add("name")

    alias_hits = max(
        (_token_hits(alias, tokens) for alias in data["aliases"]),
        default=0,
    )
    if alias_hits:
        score += alias_hits * 18
        matched_on.add("alias")

    summary_hits = _token_hits(row.get("summary"), tokens)
    if summary_hits:
        score += summary_hits * 10
        matched_on.add("summary")

    canonical_hits = _token_hits(row.get("canonical_key"), tokens)
    if canonical_hits:
        score += canonical_hits * 8
        matched_on.add("canonical_key")

    for claim in data["claims"]:
        hits = _token_hits(claim.get("claim_text"), tokens)
        if phrase and _contains(claim.get("claim_text"), phrase):
            score += 45
            matched_on.add("claim")
        if hits:
            score += min(hits, 3) * 12
            matched_on.add("claim")

    for source in data["sources"]:
        searchable = " ".join(
            str(source.get(key) or "")
            for key in ("title", "author", "platform", "source_url")
        )
        hits = _token_hits(searchable, tokens)
        if phrase and _contains(searchable, phrase):
            score += 25
            matched_on.add("source")
        if hits:
            score += min(hits, 3) * 6
            matched_on.add("source")

    for relation in data["relations"]:
        searchable = " ".join(
            str(relation.get(key) or "")
            for key in ("subject_name", "object_name", "predicate")
        )
        hits = _token_hits(searchable, tokens)
        if phrase and _contains(searchable, phrase):
            score += 25
            matched_on.add("relation")
        if hits:
            score += min(hits, 3) * 7
            matched_on.add("relation")

    return score, sorted(matched_on)


def _relevance(item, fields, phrase, tokens):
    searchable = " ".join(str(item.get(field) or "") for field in fields)
    score = 0
    if phrase and _contains(searchable, phrase):
        score += 50
    score += _token_hits(searchable, tokens) * 10
    return score


def _claim_priority(claim):
    # Le statut n'est pas un score de verite. Ce tri sert seulement a ne pas
    # perdre une contestation ou une verification quand le contexte est borne.
    order = {
        "contested": 0,
        "verified": 1,
        "asserted": 2,
        "unverified": 3,
    }
    return order.get(claim.get("verification_status"), 4)


def _select_claims(claims, phrase, tokens, limit):
    ranked = sorted(
        claims,
        key=lambda item: (
            -_relevance(item, ("claim_text", "source_title"), phrase, tokens),
            _claim_priority(item),
            -int(item.get("id") or 0),
        ),
    )
    return ranked[:limit]


def _select_sources(sources, phrase, tokens, limit):
    ranked = sorted(
        sources,
        key=lambda item: (
            -_relevance(
                item,
                ("title", "author", "platform", "source_url"),
                phrase,
                tokens,
            ),
            0 if item.get("role") == "primary" else 1,
            -int(item.get("id") or 0),
        ),
    )
    return ranked[:limit]


def _select_relations(relations, phrase, tokens, limit):
    ranked = sorted(
        relations,
        key=lambda item: (
            -_relevance(
                item,
                ("related_entity_name", "predicate", "source_url"),
                phrase,
                tokens,
            ),
            -int(item.get("id") or 0),
        ),
    )
    return ranked[:limit]

# ---------------------------------------------------------------------------
# Recherche SOURCE-FIRST (V1)
#
# Pour une question de rappel personnel ("qu'est-ce que j'avais sauvegarde
# sur X ?"), le moteur principal est la recherche directe dans les contenus
# sauvegardes (contents + intelligence_sources). Les entites, claims et
# relations ne servent ensuite qu'a enrichir le resultat.
#
# Deterministe, sans LLM ni embeddings : quelques millisecondes sur le Vault.
# ---------------------------------------------------------------------------

# Mots de rappel ou de politesse : ils decrivent la demande, pas le sujet.
# Ils ne doivent jamais dominer le classement.
_RECALL_STOPWORDS = {
    # pronoms / articles / liaisons
    "a", "au", "aux", "ce", "ces", "cet", "cette", "ca", "de", "du", "d", "en",
    "et", "ou", "la", "le", "l", "les", "un", "une", "des", "j", "je", "tu",
    "il", "elle", "on", "nous", "vous", "ils", "elles", "me", "m", "moi", "te",
    "t", "toi", "se", "s", "y", "qu", "que", "qui", "quoi", "dont", "ou",
    "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa", "ses", "notre",
    "nos", "votre", "vos", "leur", "leurs", "sur", "dans", "avec", "pour",
    "par", "chez", "vers", "entre", "sans", "sous", "est", "sont", "etait",
    "c", "n", "ne", "pas", "plus", "tout", "tous", "toute", "toutes", "tres",
    "quel", "quelle", "quels", "quelles", "comment", "combien", "pourquoi",
    "quand", "chose", "choses", "moins", "trop", "bien", "vraiment", "aussi", "encore", "truc", "trucs", "quelque", "sujet", "propos",
    "concernant", "niveau", "the", "and", "for", "with", "from", "that",
    "this", "what", "about", "my", "i", "did", "have", "had", "saved",
    # verbes / formules de rappel
    "ai", "as", "avait", "avais", "avions", "aviez", "avaient", "avoir",
    "eu", "fait", "faire", "peux", "peut", "pourrais", "voudrais", "veux",
    "sauvegarde", "sauvegardes", "sauvegardee", "sauvegardees",
    "sauvegarder", "enregistre", "enregistres", "enregistree",
    "enregistrer", "garde", "gardes", "garder", "trouve", "trouves",
    "trouvee", "trouver", "retrouve", "retrouves", "retrouver", "rappelle",
    "rappeler", "souviens", "consulte", "consulter", "cherche", "chercher",
    "recherche", "rechercher", "montre", "montrer", "dis", "dire", "donne",
    "donner", "parle", "parler", "demande", "demander", "sais", "savoir",
    "resume", "resumer", "liste", "lister", "vu", "vue", "vues", "regarde",
    "regarder", "note", "noter", "ajoute", "ajouter",
    # le contenant, pas le contenu
    "vault", "video", "videos", "tiktok", "tiktoks", "reel", "reels",
    "instagram", "insta", "source", "sources", "contenu", "contenus",
    "sauvegardes", "memoire", "life", "intelligence", "agents", "agent",
    "base", "archive", "archives", "favoris", "bibliotheque",
}

# Petite table bilingue FR/EN : une bonne partie des videos sont en anglais.
# Les synonymes ne pesent que partiellement (voir _SYNONYM_WEIGHT).
_SYNONYMS = {
    "tdah": ("adhd",),
    "adhd": ("tdah",),
    "sante": ("health", "medical", "medecin"),
    # Pas de "save" : sur TikTok il s'agit surtout de "save this reel".
    "economiser": ("economie", "reduire", "reduction", "consommation",
                   "cheaper"),
    "economie": ("economiser", "reduire", "reduction"),
    "reduire": ("reduction", "economiser", "reduce"),
    "token": ("tokens",),
    "outil": ("tool", "tools", "logiciel", "app"),
    "prospection": ("prospect", "prospects", "leads", "lead", "prospecter"),
    "prospect": ("prospection", "leads", "lead"),
    "automatiser": ("automatisation", "automation", "automate", "workflow"),
    "automatisation": ("automatiser", "automation", "automate", "workflow"),
    "gratuit": ("free", "gratuite", "gratuits", "gratuites"),
    "formation": ("formations", "course", "courses", "certification"),
    "site": ("website", "sites"),
    "hebergement": ("hebergeur", "hosting", "serveur", "server"),
    "securite": ("security", "pentest", "pentester", "hacker"),
    "argent": ("money", "finance", "revenu", "revenus"),
    "immobilier": ("villa", "appartement", "maison", "logement", "sci", "spv",
                   "real", "estate"),
}
_SYNONYM_WEIGHT = 0.6
# Traductions exactes : quasi equivalentes au terme d'origine.
_TRANSLATIONS = {("tdah", "adhd"), ("adhd", "tdah"), ("token", "tokens"),
                 ("sante", "health")}
_TRANSLATION_WEIGHT = 0.9
# Plusieurs variantes d'un meme terme ne s'additionnent pas : seule la
# meilleure compte, les autres n'apportent qu'un petit bonus.
_VARIANT_BONUS = 0.15
# Noms generiques ("des outils pour...", "des astuces sur...") : ils disent
# le type de chose cherchee, pas le sujet. Poids reduit, jamais nul.
_GENERIC_TOPIC_WORDS = {
    "outil", "outils", "tool", "tools", "astuce", "astuces", "conseil",
    "conseils", "methode", "methodes", "idee", "idees", "ressource",
    "ressources", "info", "infos", "information", "informations", "app",
    "apps", "application", "applications", "logiciel", "logiciels",
    "technique", "techniques", "solution", "solutions", "tips",
}
_GENERIC_WEIGHT = 0.4

# Poids par champ : le titre et les descriptions pesent plus que le transcript.
_FIELD_WEIGHTS = (
    ("title", 5.0),
    ("description", 3.5),
    ("taxonomy", 3.0),     # categorie, sous-themes, tags
    ("resources", 3.0),    # outils / ressources extraits de la fiche
    ("author", 1.5),
    ("transcript", 1.0),
)
_STRONG_FIELDS = {"title", "description", "taxonomy", "resources"}

_SUFFIXES = (
    "issements", "issement", "ements", "ement", "ations", "ation",
    "atrices", "atrice", "ateurs", "ateur", "ables", "able",
    "euses", "euse", "eurs", "eur", "ions", "iers", "ier", "ers", "er",
    "ees", "ee", "ez", "es", "e", "s", "x",
)


def _stem(word):
    """Racinisation francaise legere et previsible (pas de dependance)."""
    if len(word) <= 4:
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _words(text):
    return _fold(text).split()


class _FieldIndex:
    """Index de mots racinises d'un champ texte."""

    __slots__ = ("stems", "exact", "text")

    def __init__(self, text):
        words = _words(text)
        self.text = " ".join(words)
        self.exact = set(words)
        self.stems = {_stem(word) for word in words if len(word) >= 3}

    def matches(self, term):
        stem = _stem(term)
        if len(term) < 4 or len(stem) < 4:
            # Termes courts (RTK, IA, SEO...) : mot exact seulement, pour eviter
            # que "ia" matche "via" ou "media".
            return term in self.exact
        if term in self.exact or stem in self.stems:
            return True
        # Prefixe commun suffisamment long : prospect <-> prospection,
        # economis <-> economise.
        for candidate in self.stems:
            shorter = min(len(candidate), len(stem))
            if shorter >= 5 and (
                candidate.startswith(stem) or stem.startswith(candidate)
            ):
                return True
        return False


def query_terms(q):
    """
    Termes utiles d'une question naturelle, sans les mots de rappel.

    Renvoie une liste de dicts {term, weight, origin} ; origin vaut le terme
    de la question dont un synonyme est issu.
    """
    _, tokens = _query_parts(q)
    base = []
    for token in tokens:
        if token in _RECALL_STOPWORDS or token in _STOPWORDS:
            continue
        if token.isdigit() and len(token) < 3:
            continue
        if token not in base:
            base.append(token)

    terms = [
        {
            "term": token,
            "weight": _GENERIC_WEIGHT if token in _GENERIC_TOPIC_WORDS else 1.0,
            "origin": token,
        }
        for token in base
    ]
    seen = set(base)
    for token in base:
        for key in {token, _stem(token)}:
            for synonym in _SYNONYMS.get(key, ()):
                if synonym not in seen:
                    seen.add(synonym)
                    terms.append({
                        "term": synonym,
                        "weight": (
                            _TRANSLATION_WEIGHT
                            if (key, synonym) in _TRANSLATIONS
                            else _SYNONYM_WEIGHT
                        ) * (
                            _GENERIC_WEIGHT
                            if token in _GENERIC_TOPIC_WORDS
                            else 1.0
                        ),
                        "origin": token,
                    })
    return terms


def _safe_json(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _clean(value):
    return " ".join(str(value or "").split())


def _cut(text, max_chars):
    text = _clean(text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "..."


def _best_description(row):
    # La description manuelle de l'utilisateur est prioritaire.
    for field in ("description_manual", "description_ai", "description_original"):
        value = _clean(row.get(field))
        if value:
            return value
    return ""


def _excerpt_around(text, terms, max_chars=240):
    """
    Extrait du transcript : la phrase qui contient le plus de termes de la
    question (plus la precedente si elle est tres courte).
    """
    clean = _clean(text)
    if not clean:
        return ""
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", clean)
        if part.strip()
    ]
    probes = [_FieldIndex(sentence) for sentence in sentences]
    best_index, best_hits = None, 0
    for index, probe in enumerate(probes):
        hits = sum(1 for term in terms if probe.matches(term))
        if hits > best_hits:
            best_index, best_hits = index, hits
    if best_index is None:
        return _cut(clean, max_chars)
    excerpt = sentences[best_index]
    if best_index > 0 and len(sentences[best_index - 1]) < 60:
        excerpt = sentences[best_index - 1] + " " + excerpt
    if len(excerpt) < 120 and best_index + 1 < len(sentences):
        excerpt += " " + sentences[best_index + 1]
    return _cut(excerpt, max_chars)


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone())


def _load_documents(conn):
    """
    Charge les sources sauvegardees avec leurs champs texte.

    Robuste aux schemas partiels (bases de test, anciennes versions) : seules
    les colonnes presentes sont lues. Fonctionne avec sqlite3.Row comme avec
    des tuples, car les lignes sont converties via cursor.description.
    """
    source_cols = _table_columns(conn, "intelligence_sources")
    content_cols = _table_columns(conn, "contents") if _table_exists(conn, "contents") else set()

    select = [
        "s.id AS source_id",
        "s.source_url AS source_url",
    ]
    for col in ("platform", "source_type", "title", "author", "content_id",
                "captured_at", "metadata"):
        if col in source_cols:
            select.append(f"s.{col} AS s_{col}")

    join = ""
    if content_cols and "content_id" in source_cols:
        join = "LEFT JOIN contents c ON c.id = s.content_id"
        for col in ("title", "author", "platform", "source_url",
                    "description_original", "description_ai",
                    "description_manual", "transcript", "category"):
            if col in content_cols:
                select.append(f"c.{col} AS c_{col}")

    cursor = conn.execute(
        f"SELECT {', '.join(select)} FROM intelligence_sources s {join}"
    )
    columns = [item[0] for item in cursor.description]
    documents = [dict(zip(columns, tuple(raw))) for raw in cursor.fetchall()]

    content_ids = [d.get("s_content_id") for d in documents if d.get("s_content_id")]
    taxonomy = {}
    resources = {}
    if content_ids:
        def extra(sql):
            out = {}
            for content_id, name in conn.execute(sql).fetchall():
                out.setdefault(content_id, []).append(name)
            return out

        if _table_exists(conn, "content_tags") and _table_exists(conn, "tags"):
            for key, names in extra(
                "SELECT ct.content_id, t.name FROM content_tags ct "
                "JOIN tags t ON t.id = ct.tag_id"
            ).items():
                taxonomy.setdefault(key, []).extend(names)
        if _table_exists(conn, "content_subthemes") and _table_exists(conn, "subthemes"):
            for key, names in extra(
                "SELECT cs.content_id, st.name FROM content_subthemes cs "
                "JOIN subthemes st ON st.id = cs.subtheme_id"
            ).items():
                taxonomy.setdefault(key, []).extend(names)
        if _table_exists(conn, "resources") and "content_id" in _table_columns(conn, "resources"):
            resources = extra(
                "SELECT content_id, name FROM resources WHERE content_id IS NOT NULL"
            )

    for doc in documents:
        content_id = doc.get("s_content_id")
        metadata = _safe_json(doc.get("s_metadata"))
        meta_terms = []
        for key in ("category", "subthemes", "tags"):
            value = metadata.get(key)
            if isinstance(value, list):
                meta_terms.extend(str(item) for item in value)
            elif value:
                meta_terms.append(str(value))
        doc["tags"] = list(dict.fromkeys(taxonomy.get(content_id, []) + meta_terms))
        doc["resources"] = resources.get(content_id, [])
        doc["metadata"] = metadata
    return documents


def _doc_fields(doc):
    title = doc.get("c_title") or doc.get("s_title") or ""
    return {
        "title": title,
        "description": " ".join(
            _clean(doc.get(f"c_{field}"))
            for field in ("description_manual", "description_ai",
                          "description_original")
        ),
        "taxonomy": " ".join(
            [_clean(doc.get("c_category"))] + list(doc.get("tags") or [])
        ),
        "resources": " ".join(doc.get("resources") or []),
        "author": doc.get("s_author") or doc.get("c_author") or "",
        "transcript": doc.get("c_transcript") or "",
    }


def search_saved_sources(conn, q, *, limit=8, min_relative=0.25):
    """
    Recherche directe dans les sources sauvegardees.

    Score = somme, pour chaque terme de la question, de
        idf(terme) x poids du meilleur champ matche (+ bonus multi-champs)
    puis multiplie par la couverture de la question (part des termes utiles
    retrouves). Les mots de rappel ("sauvegarde", "mes videos"...) sont
    retires avant le calcul ; les termes presents partout (ex. "ia") pesent
    peu grace a l'idf.

    Chaque resultat contient au minimum : source_id, content_id, title, url,
    platform, author, category, snippet, matched_tokens, score.
    """
    terms = query_terms(q)
    if not terms:
        return []

    documents = _load_documents(conn)
    if not documents:
        return []

    indexed = []
    for doc in documents:
        fields = {
            name: _FieldIndex(value)
            for name, value in _doc_fields(doc).items()
        }
        indexed.append((doc, fields))

    total = len(indexed)
    df = {}
    for item in terms:
        term = item["term"]
        df[term] = sum(
            1 for _, fields in indexed
            if any(index.matches(term) for index in fields.values())
        )

    def idf(term):
        return math.log(1.0 + (total + 1.0) / (df.get(term, 0) + 0.5))

    origins = {}
    for item in terms:
        if item["term"] == item["origin"]:
            origins[item["origin"]] = idf(item["term"]) * item["weight"]
        else:
            origins.setdefault(item["origin"], 0.0)
    origin_mass = sum(origins.values()) or 1.0

    ranked = []
    for doc, fields in indexed:
        per_origin = {}
        matched_tokens = []
        matched_fields = set()
        covered = {}
        strong_origins = set()
        for item in terms:
            term = item["term"]
            hit_fields = [
                name for name, _weight in _FIELD_WEIGHTS
                if fields[name].matches(term)
            ]
            if not hit_fields:
                continue
            weights = dict(_FIELD_WEIGHTS)
            best = max(weights[name] for name in hit_fields)
            spread = 0.25 * (len(hit_fields) - 1)
            contribution = idf(term) * item["weight"] * (best + spread)
            per_origin.setdefault(item["origin"], []).append(contribution)
            matched_tokens.append(term)
            matched_fields.update(hit_fields)
            origin = item["origin"]
            base_weight = next(
                t["weight"] for t in terms if t["term"] == origin
            )
            covered[origin] = max(
                covered.get(origin, 0.0),
                origins[origin] * min(1.0, item["weight"] / base_weight),
            )
            if set(hit_fields) & _STRONG_FIELDS:
                strong_origins.add(origin)

        if not matched_tokens:
            continue

        score = 0.0
        for contributions in per_origin.values():
            contributions.sort(reverse=True)
            score += contributions[0] + _VARIANT_BONUS * sum(contributions[1:])

        coverage = sum(covered.values()) / origin_mass
        score *= 0.4 + 0.6 * coverage

        # Un simple mot isole dans un long transcript n'est pas un resultat
        # fiable : il faut un champ fort, ou plusieurs termes de la question.
        if not strong_origins and len(covered) < 2:
            continue

        ranked.append({
            "doc": doc,
            "score": round(score, 2),
            "coverage": round(coverage, 2),
            "matched_tokens": matched_tokens,
            "matched_fields": sorted(
                matched_fields,
                key=lambda name: [n for n, _ in _FIELD_WEIGHTS].index(name),
            ),
        })

    ranked.sort(key=lambda item: (-item["score"], -int(item["doc"]["source_id"])))
    if not ranked:
        return []

    top = ranked[0]["score"]
    ranked = [item for item in ranked if item["score"] >= top * min_relative]

    # Deduplication des doublons stricts (meme URL ou meme titre + auteur).
    results = []
    seen = set()
    for item in ranked:
        keys = _dedupe_keys(item["doc"])
        if keys & seen:
            continue
        seen |= keys
        results.append(_result_from_doc(item["doc"], item))
        if len(results) >= limit:
            break
    return results


def _dedupe_keys(doc):
    title = doc.get("c_title") or doc.get("s_title") or ""
    author = doc.get("s_author") or doc.get("c_author") or ""
    return {
        ("url", doc.get("source_url")),
        ("title", _fold(title), _fold(author)),
    }


def _result_from_doc(doc, item, excerpt=None):
    """Resultat cite (provenance complete) a partir d'un document charge."""
    title = doc.get("c_title") or doc.get("s_title") or ""
    author = doc.get("s_author") or doc.get("c_author") or ""
    description = _best_description({
        field: doc.get(f"c_{field}")
        for field in ("description_manual", "description_ai",
                      "description_original")
    })
    # Champ d'origine de l'extrait : une description MANUELLE est une note
    # personnelle et ne doit jamais partir vers un provider cloud.
    description_field = next(
        (field for field in ("description_manual", "description_ai", "description_original")
         if _clean(doc.get(f"c_{field}"))),
        None,
    )
    transcript_excerpt = excerpt or ""
    if not transcript_excerpt and "transcript" in item["matched_fields"]:
        transcript_excerpt = _excerpt_around(
            doc.get("c_transcript"), item["matched_tokens"]
        )
    snippet = _cut(description, 320) or transcript_excerpt or _cut(
        doc.get("c_transcript"), 220
    )
    return {
        "source_id": int(doc["source_id"]),
        "id": int(doc["source_id"]),
        "content_id": doc.get("s_content_id"),
        "title": title,
        "url": doc.get("source_url") or doc.get("c_source_url"),
        "source_url": doc.get("source_url") or doc.get("c_source_url"),
        "platform": doc.get("s_platform") or doc.get("c_platform"),
        "source_type": doc.get("s_source_type"),
        "author": author or None,
        "category": doc.get("c_category"),
        "tags": doc.get("tags") or [],
        "resources": doc.get("resources") or [],
        "captured_at": doc.get("s_captured_at"),
        "snippet": snippet,
        "snippet_field": (
            description_field if _cut(description, 320) else
            ("transcript" if snippet else None)
        ),
        "transcript_excerpt": transcript_excerpt,
        "matched_tokens": item["matched_tokens"],
        "matched_fields": item["matched_fields"],
        "score": item["score"],
        "coverage": item["coverage"],
        "match": {
            "score": item["score"],
            "matched_tokens": item["matched_tokens"],
            "matched_fields": item["matched_fields"],
            "coverage": item["coverage"],
        },
    }


# ---------------------------------------------------------------------------
# Recherche HYBRIDE (V2) : lexical + semantique local.
#
# - le lexical reste le moteur principal et garde ses filtres ;
# - le semantique reordonne les resultats lexicaux (fusion ponderee) ;
# - une source trouvee UNIQUEMENT par le sens n'est admise que si elle se
#   detache nettement (z >= vault_embeddings.SEMANTIC_ONLY_MIN_Z, corpus
#   suffisant) et elle est marquee match_type="semantic" (a verifier) ;
# - embeddings indisponibles, index vide ou trop lent : fallback lexical.
# ---------------------------------------------------------------------------

LEXICAL_WEIGHT = 0.75
SEMANTIC_WEIGHT = 0.25
MAX_SEMANTIC_ONLY = 2
# Veto semantique d'une correspondance lexicale faible : plus l'indice
# lexical est mince, plus la confirmation semantique doit etre forte.
# (couverture maximale, z minimal requis pour garder le resultat)
SEMANTIC_VETO_TIERS = ((0.25, 2.0), (0.5, 1.0))


def _vetoed(coverage, z):
    for max_coverage, min_z in SEMANTIC_VETO_TIERS:
        if coverage < max_coverage:
            return z < min_z
    return False


def _semantic_norm(z):
    # z = 1 -> 0 ; z = 3 -> 1 (bornes du banc d'essai).
    return max(0.0, min(1.0, (z - 1.0) / 2.0))


def _run_with_timeout(func, timeout):
    import threading

    box = {}

    def target():
        try:
            box["value"] = func()
        except Exception as exc:  # noqa: BLE001 - remonte en fallback lexical
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None, "timeout"
    if "error" in box:
        return None, f"error:{type(box['error']).__name__}"
    return box.get("value"), "ok"


def search_sources_hybrid(conn, q, *, limit=8, semantic="auto", backend=None,
                          semantic_timeout=8.0):
    """
    Recherche hybride. Retourne (results, info) ; chaque resultat garde la
    provenance complete et un bloc "retrieval" explicite.
    """
    import vault_embeddings as embeddings

    lexical = search_saved_sources(conn, q, limit=max(limit * 2, 12))
    info = {"mode": "lexical", "semantic_status": "disabled"}
    if semantic == "off":
        return lexical[:limit], info

    # Lecture de l'index dans CE thread (connexion SQLite non partageable),
    # puis seul l'encodage de la question passe sous echeance.
    backend = backend or embeddings.default_backend()
    rows = embeddings.load_index(conn, getattr(backend, "model", embeddings.MODEL_ID))
    if not rows or backend is None:
        info["semantic_status"] = "unavailable"
        info["mode"] = "lexical_fallback"
        return lexical[:limit], info
    scored, status = _run_with_timeout(
        lambda: embeddings.score_rows(rows, backend.embed_query(q)),
        semantic_timeout,
    )
    if status != "ok" or not scored:
        info["semantic_status"] = "unavailable" if status == "ok" else status
        info["mode"] = "lexical_fallback"
        return lexical[:limit], info

    scores = scored["scores"]
    corpus = scored["corpus_size"]
    threshold = embeddings.semantic_threshold(
        conn, getattr(backend, "model", embeddings.MODEL_ID))
    info.update({"mode": "hybrid", "semantic_status": "ok", "corpus_size": corpus,
                 "semantic_only_min_z": round(threshold, 3)})

    results = []
    vetoed = []
    top_lexical = lexical[0]["score"] if lexical else 0.0
    for item in lexical:
        sem = scores.get(item["source_id"])
        # Correspondance lexicale FAIBLE (moins de la moitie de la question)
        # et semantique clairement sans rapport : faux positif ecarte
        # (ex. "resultats du match de football" -> "voila le resultat").
        if sem and _vetoed(item["coverage"], sem["z"]):
            vetoed.append(item["source_id"])
            continue
        lex_norm = item["score"] / top_lexical if top_lexical else 0.0
        sem_norm = _semantic_norm(sem["z"]) if sem else 0.0
        item = dict(item)
        item["retrieval"] = {
            "match_type": "hybrid" if sem and sem_norm > 0 else "lexical",
            "lexical_score": item["score"],
            "semantic_cos": round(sem["cos"], 4) if sem else None,
            "semantic_z": round(sem["z"], 2) if sem else None,
            "fused": round(LEXICAL_WEIGHT * lex_norm + SEMANTIC_WEIGHT * sem_norm, 4),
        }
        results.append(item)

    # Sources absentes du lexical, admises seulement si elles se detachent.
    if corpus >= embeddings.MIN_CORPUS_FOR_SEMANTIC_ONLY:
        known = {item["source_id"] for item in results}
        candidates = sorted(
            (
                (source_id, sem) for source_id, sem in scores.items()
                if source_id not in known and sem["z"] >= threshold
            ),
            key=lambda pair: -pair[1]["z"],
        )[:MAX_SEMANTIC_ONLY]
        if candidates:
            documents = {int(doc["source_id"]): doc for doc in _load_documents(conn)}
            seen = set()
            for item in results:
                doc = documents.get(item["source_id"])
                if doc:
                    seen |= _dedupe_keys(doc)
            for source_id, sem in candidates:
                doc = documents.get(source_id)
                if not doc or _dedupe_keys(doc) & seen:
                    continue
                seen |= _dedupe_keys(doc)
                result = _result_from_doc(
                    doc,
                    {"score": 0.0, "coverage": 0.0, "matched_tokens": [], "matched_fields": []},
                    excerpt=_cut(sem["chunk_text"], 240),
                )
                result["retrieval"] = {
                    "match_type": "semantic",
                    "lexical_score": None,
                    "semantic_cos": round(sem["cos"], 4),
                    "semantic_z": round(sem["z"], 2),
                    # Toujours sous un resultat lexical solide.
                    "fused": round(0.5 * _semantic_norm(sem["z"]), 4),
                }
                results.append(result)

    info["semantic_vetoed"] = vetoed
    results.sort(key=lambda item: (-item["retrieval"]["fused"], -item["source_id"]))
    return results[:limit], info


def _entities_for_sources(conn, source_ids, limit=8):
    """Enrichissement : entites deja liees aux sources trouvees."""
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT e.id, e.name, e.entity_type, e.attention_status,
               es.source_id, es.role
          FROM intelligence_entity_sources es
          JOIN intelligence_entities e ON e.id = es.entity_id
         WHERE es.source_id IN ({placeholders})
         ORDER BY e.id
        """,
        tuple(source_ids),
    ).fetchall()
    by_entity = {}
    for row in rows:
        item = dict(row)
        entry = by_entity.setdefault(item["id"], {
            "id": item["id"],
            "name": item["name"],
            "entity_type": item["entity_type"],
            "attention_status": item["attention_status"],
            "source_ids": [],
        })
        if item["source_id"] not in entry["source_ids"]:
            entry["source_ids"].append(item["source_id"])
    ordered = sorted(
        by_entity.values(),
        key=lambda item: (-len(item["source_ids"]), item["id"]),
    )
    return ordered[:limit]


class _RowFactory:
    """
    Impose sqlite3.Row le temps d'un appel, puis restaure l'etat d'origine.

    Tout le projet (vault_db.connect, mentor_*, tests) travaille avec
    sqlite3.Row ; une connexion brute sqlite3.connect() renvoie des tuples.
    On normalise ici, a l'entree publique, plutot que ligne par ligne.
    """

    def __init__(self, conn):
        self.conn = conn
        self.previous = None

    def __enter__(self):
        self.previous = self.conn.row_factory
        if self.previous is not sqlite3.Row:
            self.conn.row_factory = sqlite3.Row
        return self.conn

    def __exit__(self, *exc):
        self.conn.row_factory = self.previous
        return False


def build_context_pack(
    conn,
    q,
    *,
    entity_limit=6,
    claims_per_entity=6,
    sources_per_entity=4,
    relations_per_entity=6,
    direct_source_limit=8,
    semantic="off",
    semantic_backend=None,
):
    """
    Construit un paquet compact pour une IA en aval.

    1. SOURCES : recherche directe dans les sources sauvegardees (moteur
       principal pour les questions de rappel personnel).
    2. ENTITES : enrichissement (entites liees aux sources trouvees + recherche
       par nom/alias/claims), jamais moteur principal.

    Aucun resume n'est genere ici. Le summary renvoye est uniquement le summary
    actif deja stocke sur l'entite.
    """
    with _RowFactory(conn):
        return _build_context_pack(
            conn,
            q,
            entity_limit=entity_limit,
            claims_per_entity=claims_per_entity,
            sources_per_entity=sources_per_entity,
            relations_per_entity=relations_per_entity,
            direct_source_limit=direct_source_limit,
            semantic=semantic,
            semantic_backend=semantic_backend,
        )


def _claims_capability(conn):
    claims = conn.execute("SELECT COUNT(*) FROM intelligence_claims").fetchone()[0]
    relations = conn.execute("SELECT COUNT(*) FROM intelligence_relations").fetchone()[0]
    structured = 0
    if _table_exists(conn, "intelligence_claim_structure"):
        structured = conn.execute(
            "SELECT COUNT(*) FROM intelligence_claim_structure WHERE superseded_at IS NULL"
        ).fetchone()[0]
    return {
        "claims_count": claims,
        "structured_claims_count": structured,
        "relations_count": relations,
        # Sans claims STRUCTURES (sujet/predicat/objet/portee/date), aucune
        # contradiction ne peut etre affirmee : pas de faux positif.
        "contradiction_detection": (
            "available" if structured else "unavailable_no_structured_claims"
        ),
    }


def _build_context_pack(
    conn,
    q,
    *,
    entity_limit,
    claims_per_entity,
    sources_per_entity,
    relations_per_entity,
    direct_source_limit,
    semantic="off",
    semantic_backend=None,
):
    phrase, tokens = _query_parts(q)
    # Couche en lecture : on ne cree le schema que s'il manque (base neuve),
    # ce qui permet aussi les connexions SQLite en lecture seule.
    if not _table_exists(conn, "intelligence_claims"):
        intelligence.ensure_schema(conn)
    # Les mots de rappel ("sauvegarde", "trouve"...) ne doivent pas non plus
    # piloter la recherche d'entites.
    entity_tokens = [
        token for token in tokens
        if token not in _RECALL_STOPWORDS
    ]

    source_limit = max(1, min(int(direct_source_limit), 20))
    if semantic == "off":
        direct_sources = search_saved_sources(conn, q, limit=source_limit)
        retrieval_info = {"mode": "lexical", "semantic_status": "disabled"}
    else:
        direct_sources, retrieval_info = search_sources_hybrid(
            conn, q, limit=source_limit, semantic=semantic, backend=semantic_backend,
        )

    entity_limit = max(1, min(int(entity_limit), 20))
    claims_per_entity = max(1, min(int(claims_per_entity), 20))
    sources_per_entity = max(1, min(int(sources_per_entity), 20))
    relations_per_entity = max(1, min(int(relations_per_entity), 20))

    ranked = []
    if entity_tokens:
        search_data = _entity_search_data(conn)
        for entity_id, data in search_data.items():
            score, matched_on = _score_entity(data, phrase, entity_tokens)
            if score > 0:
                ranked.append((score, entity_id, matched_on))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    ranked = ranked[:entity_limit]

    results = []
    for score, entity_id, matched_on in ranked:
        full = query_layer.get_entity(conn, entity_id)
        if full is None:
            continue

        claims = _select_claims(
            full.get("claims", []), phrase, entity_tokens, claims_per_entity,
        )
        sources = _select_sources(
            full.get("sources", []), phrase, entity_tokens, sources_per_entity,
        )
        relations = _select_relations(
            full.get("relations", []), phrase, entity_tokens, relations_per_entity,
        )

        results.append(
            {
                "entity": {
                    "id": full["id"],
                    "entity_type": full["entity_type"],
                    "canonical_key": full["canonical_key"],
                    "name": full["name"],
                    "url": full.get("url"),
                    "summary": full.get("summary"),
                    "attention_status": full["attention_status"],
                    "aliases": full.get("aliases", []),
                },
                "match": {
                    "score": score,
                    "matched_on": matched_on,
                },
                "epistemic": full.get("epistemic", {}),
                "claims": claims,
                "sources": sources,
                "relations": relations,
            }
        )

    linked_entities = _entities_for_sources(
        conn, [item["source_id"] for item in direct_sources[:5]],
    )

    return {
        "query": q.strip(),
        "retrieval": "source_first",
        "retrieval_info": retrieval_info,
        "query_terms": [item["term"] for item in query_terms(q)],
        "source_result_count": len(direct_sources),
        "sources": direct_sources,
        "linked_entities": linked_entities,
        "result_count": len(results),
        "entities": results,
        "capabilities": _claims_capability(conn),
        "epistemic_rules": {
            "asserted": (
                "The saved source asserts this claim; it is not independently verified."
            ),
            "unverified": (
                "The claim is explicitly tracked as not verified."
            ),
            "verified": (
                "The claim has a separate verification source recorded in the Vault."
            ),
            "contested": (
                "The claim has conflicting or contesting evidence and must not be "
                "presented as settled."
            ),
        },
        "generation": {
            "llm_used": False,
            "embeddings_used": False,
            "raw_model_results_included": False,
        },
    }
