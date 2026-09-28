#!/usr/bin/env python3
"""
vault_claims.py - Claims STRUCTURES avec provenance (Life Intelligence V2).

Etend l'extraction V3 (vault_analysis.py) sans la remplacer :
- chaque affirmation garde sujet / predicat / objet, polarite, type (fait,
  hypothese, conseil, opinion, prediction, experience personnelle), modalite,
  confiance, type de preuve, EXTRAIT REEL de la source, portee, temporalite,
  provenance et statut de validation ;
- le modele (Qwen local, ou un extracteur cloud optionnel) PROPOSE ; un
  validateur deterministe DECIDE :
    * l'extrait cite doit exister dans la source (sinon rejet) ;
    * une valeur chiffree absente de l'extrait est rejetee (valeur inventee) ;
    * un "fait" portant des marqueurs d'opinion / conseil / prediction /
      experience / hypothese est reclasse (jamais l'inverse) ;
    * la modalite ne peut pas etre plus forte que l'extrait ;
    * sante / finance / droit -> toujours needs_review, confiance plafonnee ;
    * structure incomplete ou sujet non ancre -> needs_review ;
- aucune entite n'est creee ; aucun claim n'est supprime (les anciennes
  extractions sont marquees superseded_at) ;
- idempotent : une source deja extraite avec le meme texte et la meme version
  n'est pas retraitee.

Traitement lourd : a lancer hors du temps interactif
(python vault_claims.py --db vault.db --next --limit 5).
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

import vault_analysis as analysis
import vault_intelligence as intelligence

EXTRACTION_VERSION = "claims-v2.1"

CLAIM_KINDS = (
    "fact", "hypothesis", "advice", "opinion", "prediction", "personal_experience",
)
MODALITIES = ("asserted", "probable", "possible", "conditional")
# Ordre de force : une modalite ne peut qu'etre affaiblie par le validateur.
_MODALITY_STRENGTH = {"asserted": 3, "probable": 2, "conditional": 1, "possible": 1}
EVIDENCE_TYPES = (
    "study", "measurement", "demonstration", "testimony", "expert_statement",
    "promotional", "unspecified",
)
ACCEPT_MIN_CONFIDENCE = 0.6

SCHEMA = """
CREATE TABLE IF NOT EXISTS intelligence_claim_extractions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      INTEGER NOT NULL REFERENCES intelligence_sources(id) ON DELETE CASCADE,
    fingerprint    TEXT NOT NULL,
    extractor      TEXT NOT NULL,
    model          TEXT,
    version        TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    claims_written INTEGER NOT NULL DEFAULT 0,
    rejected       TEXT NOT NULL DEFAULT '[]',
    raw_result     TEXT NOT NULL DEFAULT '{}',
    error          TEXT,
    elapsed_s      REAL,
    superseded_at  TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_extractions_source
    ON intelligence_claim_extractions(source_id, fingerprint, version);

CREATE TABLE IF NOT EXISTS intelligence_claim_structure (
    claim_id          INTEGER PRIMARY KEY REFERENCES intelligence_claims(id) ON DELETE CASCADE,
    source_id         INTEGER NOT NULL REFERENCES intelligence_sources(id) ON DELETE CASCADE,
    content_id        INTEGER,
    extraction_id     INTEGER REFERENCES intelligence_claim_extractions(id) ON DELETE SET NULL,
    subject           TEXT NOT NULL,
    subject_key       TEXT NOT NULL,
    subject_entity_id INTEGER REFERENCES intelligence_entities(id) ON DELETE SET NULL,
    predicate         TEXT NOT NULL,
    predicate_key     TEXT NOT NULL,
    object_value      TEXT,
    object_key        TEXT,
    object_number     REAL,
    object_unit       TEXT,
    polarity          TEXT NOT NULL CHECK (polarity IN ('affirmative', 'negative')),
    claim_kind        TEXT NOT NULL CHECK (claim_kind IN
                        ('fact', 'hypothesis', 'advice', 'opinion', 'prediction', 'personal_experience')),
    modality          TEXT NOT NULL CHECK (modality IN ('asserted', 'probable', 'possible', 'conditional')),
    confidence        REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_type     TEXT NOT NULL,
    evidence_excerpt  TEXT NOT NULL,
    evidence_field    TEXT NOT NULL,
    evidence_match    TEXT NOT NULL CHECK (evidence_match IN ('exact', 'fuzzy')),
    scope             TEXT,
    scope_key         TEXT,
    temporal_ref      TEXT,
    temporal_year     INTEGER,
    observed_at       TEXT,
    sensitivity       TEXT,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('accepted', 'needs_review')),
    review_reasons    TEXT NOT NULL DEFAULT '[]',
    provenance        TEXT NOT NULL,
    user_validated    INTEGER NOT NULL DEFAULT 0,
    superseded_at     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_structure_subject
    ON intelligence_claim_structure(subject_key, predicate_key);
CREATE INDEX IF NOT EXISTS idx_claim_structure_source
    ON intelligence_claim_structure(source_id);
"""


_NULL_SCOPES = {"none", "null", "n a", "na", "general", "generale", "aucun", "aucune",
                "tous", "tout le monde", "everyone", "all", "unspecified", "non precise",
                "non specifie", "global"}


class ClaimExtractionError(RuntimeError):
    pass


def ensure_schema(conn):
    intelligence.ensure_schema(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_fold = analysis._fold_for_evidence

_STOP = {
    "le", "la", "les", "l", "un", "une", "des", "de", "du", "d", "et", "ou", "a",
    "au", "aux", "en", "pour", "par", "sur", "avec", "dans", "ce", "cet", "cette",
    "ces", "son", "sa", "ses", "leur", "leurs", "the", "a", "an", "of", "to", "for",
    "in", "on", "with", "and", "or", "its", "their", "est", "sont", "is", "are",
    "que", "qui", "tes", "ton", "ta", "mes", "mon", "ma", "vos", "votre", "nos",
}

_SUFFIXES = ("ements", "ement", "ations", "ation", "ateurs", "ateur", "euses",
             "euse", "eurs", "eur", "ions", "ers", "er", "ees", "ee", "ez",
             "es", "e", "s", "x")


def _stem(word):
    if len(word) <= 4:
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _content_tokens(text):
    return [_stem(w) for w in _fold(text).split() if w not in _STOP and len(w) > 1]


def normalize_key(text):
    """Cle comparable : racines des mots porteurs, dans l'ordre."""
    return " ".join(_content_tokens(text))


# Familles de predicats : deux formulations de la meme relation doivent
# pouvoir etre comparees ("reduit" / "diminue" / "reduces").
PREDICATE_FAMILIES = {
    "reduce": ("redui", "reduir", "reduit", "diminu", "baiss", "economis", "economi",
               "reduc", "decreas", "lower", "cut", "save", "sav"),
    "increase": ("augment", "amelior", "boost", "increas", "improv", "rais", "accel"),
    "cost": ("cout", "coute", "prix", "cost", "price", "pric", "vaut", "factur", "pay"),
    "treat": ("trait", "soign", "gueri", "treat", "cure", "efficac", "effectiv"),
    "cause": ("provoqu", "caus", "entrain", "induc", "induit"),
    "require": ("requi", "necessit", "exig", "need", "doit"),
    "replace": ("remplac", "replac", "supersed", "succed"),
    "integrate": ("integr", "connect", "compatib", "work with", "fonctionn avec"),
    "offer": ("propos", "offr", "inclu", "includ", "provid", "fourni", "permet", "allow", "enabl"),
    "is": ("est", "is", "are", "sont", "etre", "be"),
}


def predicate_key(predicate):
    folded = _fold(predicate)
    tokens = folded.split()
    first = tokens[0] if tokens else ""
    for family, stems in PREDICATE_FAMILIES.items():
        for stem in stems:
            if " " in stem:
                if stem in folded:
                    return family
            elif first.startswith(stem) or any(t.startswith(stem) for t in tokens[:2]):
                return family
    return " ".join((normalize_key(predicate) or folded).split()[:4])


_NUMBER = re.compile(
    r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(%|€|\$|eur|euros?|usd|dollars?|x|fois|ms|sec|secondes?|min|minutes?|h|heures?|jours?|days?|ans?|years?|go|gb|mo|mb|k)?(?![\w])",
    re.IGNORECASE,
)


def _join_thousands(text):
    # "80 000" / "80.000" -> "80000" (sinon "80" serait lu seul).
    return re.sub(r"(?<=\d)[ \u00a0.](?=\d{3}(?!\d))", "", str(text or ""))


def evidence_numbers(text):
    return {float(n.replace(",", ".")) for n in re.findall(r"\d+(?:,\d+)?",
                                                            _join_thousands(text))}


def parse_number(text):
    match = _NUMBER.search(_join_thousands(text))
    if not match:
        return None, None
    value = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "").lower() or None
    if unit in ("euro", "euros", "eur"):
        unit = "€"
    if unit in ("dollar", "dollars", "usd"):
        unit = "$"
    return value, unit


# ---------------------------------------------------------------------------
# Indices deterministes (FR/EN)
# ---------------------------------------------------------------------------

KIND_CUES = {
    "opinion": (
        "je pense", "je trouve", "a mon avis", "selon moi", "personnellement je",
        "i think", "i believe", "in my opinion", "imo", "le meilleur",
        "la meilleure", "les meilleurs", "the best", "best ever", "incroyable",
        "genial", "de ouf", "dingue", "amazing", "insane", "game changer",
        "worst", "le pire", "trop bien", "je kiffe", "i love", "j adore",
        "revolutionnaire", "hallucinant", "magique", "perfect for", "parfait pour",
        "really shines", "must have", "indispensable",
    ),
    "personal_experience": (
        "j ai teste", "j ai essaye", "j ai utilise", "je l ai teste", "chez moi",
        "dans mon cas", "perso", "i tested", "i tried", "i used", "i ve been using",
        "on a teste", "nous avons teste", "mon experience",
    ),
    "prediction": (
        "va remplacer", "vont remplacer", "sera", "seront", "va devenir",
        "will replace", "will be", "going to", "d ici 20", "by 20", "bientot",
        "dans le futur", "in the future", "a l avenir", "prochainement",
    ),
    "advice": (
        "il faut", "tu dois", "vous devez", "je te conseille", "je vous conseille",
        "je recommande", "n hesite", "you should", "you need to", "make sure",
        "pense a", "je te recommande", "try this", "tu peux utiliser",
        "il suffit de", "abonne toi",
    ),
    "hypothesis": (
        "peut", "pourrait", "pourraient", "potentiel", "potentiellement",
        "suggere", "semble", "possiblement", "hypothese", "may", "might",
        "could", "possibly", "suggests", "appears", "seems", "potential",
    ),
}
# Ordre de priorite si plusieurs familles sont presentes.
KIND_PRIORITY = ("opinion", "personal_experience", "prediction", "advice", "hypothesis")

MODALITY_CUES = {
    "conditional": ("si tu", "si vous", "si on", "s il", "si le", "si la", "a condition",
                    "sous reserve", "if you", "if the", "provided that", "only when"),
    "possible": ("peut", "pourrait", "possible", "potentiel", "may", "might", "could", "possibly"),
    "probable": ("probablement", "semble", "suggere", "environ", "likely", "probably",
                 "seems", "suggests", "approximately", "about", "~"),
}

SCOPE_CUES = (
    "rats", "rat", "souris", "mice", "mouse", "animaux", "animals", "in vitro",
    "enfants", "children", "adolescents", "adultes", "adults", "patients",
    "femmes", "women", "hommes", "men", "seniors", "etudiants", "students",
    "debutants", "beginners", "entreprises", "companies", "startups", "freelances",
    "france", "usa", "etats unis", "europe", "canada",
)

TEMPORAL_CUES = ("aujourd hui", "actuellement", "desormais", "maintenant", "now",
                 "currently", "today", "cette annee", "this year", "depuis")

EVIDENCE_TYPE_CUES = {
    "study": ("etude", "study", "research", "recherche", "essai clinique", "clinical trial",
              "publication", "meta analyse"),
    "measurement": ("mesure", "benchmark", "test", "resultat", "measured"),
    "promotional": ("lien en bio", "link in bio", "code promo", "commente",
                    "abonne toi", "sponsor", "partenariat", "comment below"),
}


def _has_cue(folded_text, cue):
    cue_folded = _fold(cue)
    if not cue_folded:
        return "~" in folded_text
    return re.search(r"(?<![a-z0-9])" + re.escape(cue_folded) + r"(?![a-z0-9])",
                     folded_text) is not None


def detect_kinds(text):
    folded = _fold(text)
    return [kind for kind in KIND_PRIORITY
            if any(_has_cue(folded, cue) for cue in KIND_CUES[kind])]


def detect_modality(text):
    raw = str(text or "")
    folded = _fold(raw) + " "
    if any(_has_cue(folded, cue) for cue in MODALITY_CUES["conditional"]):
        return "conditional"
    if any(_has_cue(folded, cue) for cue in MODALITY_CUES["possible"]):
        return "possible"
    if "~" in raw or any(_has_cue(folded, cue) for cue in MODALITY_CUES["probable"] if cue != "~"):
        return "probable"
    return "asserted"


def detect_scope(text):
    folded = _fold(text)
    found = [cue for cue in SCOPE_CUES if _has_cue(folded, cue)]
    return ", ".join(dict.fromkeys(found)) or None


def detect_temporal(text):
    raw = str(text or "")
    years = [int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", raw)]
    folded = _fold(raw)
    cues = [cue for cue in TEMPORAL_CUES if _has_cue(folded, cue)]
    ref = None
    if years or cues:
        ref = ", ".join([str(y) for y in years] + cues)
    return ref, (max(years) if years else None)


def detect_evidence_type(text):
    folded = _fold(text)
    for kind, cues in EVIDENCE_TYPE_CUES.items():
        if any(_has_cue(folded, cue) for cue in cues):
            return kind
    return None


def detect_sensitivity(text, category=None):
    folded = _fold(f"{category or ''} {text or ''}")
    groups = {
        "health": ("sante", "health", "medical", "medecin", "medicament", "traitement",
                   "treatment", "clinique", "clinical", "patient", "diagnostic",
                   "psychiatr", "adhd", "tdah", "autisme", "autism", "dose", "therapie",
                   "maladie", "disease", "hyperactiv", "guanfacine", "clonidine"),
        "finance": ("finance", "fiscal", "impot", "tax", "banque", "bank", "credit",
                    "investissement", "investment", "patrimoine", "bourse", "crypto",
                    "spv", "rendement", "loan", "pret"),
        "legal": ("juridique", "legal", "avocat", "lawyer", "contrat", "contract",
                  "loi", "law", "tribunal", "urssaf", "rgpd", "gdpr"),
    }
    for label, cues in groups.items():
        for cue in cues:
            if re.search(r"(?<![a-z0-9])" + re.escape(cue), folded):
                return label
    return None


# ---------------------------------------------------------------------------
# Ancrage de l'extrait dans la source
# ---------------------------------------------------------------------------

def locate_evidence(evidence, fields, min_overlap=0.8):
    """
    Retrouve l'extrait cite dans la source.
    Retourne (field, excerpt_reel, "exact"|"fuzzy") ou None.
    L'extrait stocke est TOUJOURS le texte de la source, jamais la paraphrase
    du modele.
    """
    target = _fold(evidence).split()
    if len(" ".join(target)) < 8:
        return None

    def tokenized(text):
        # (jeton plie, index du mot original) : permet de rendre le passage
        # ORIGINAL de la source, ponctuation et accents compris.
        words = " ".join(str(text or "").split()).split()
        tokens = []
        for index, word in enumerate(words):
            for token in _fold(word).split():
                tokens.append((token, index))
        return words, tokens

    def with_trailing_symbols(words, last):
        # "%", "€", "$" disparaissent au pliage mais font partie de la preuve.
        while last + 1 < len(words) and not _fold(words[last + 1]):
            last += 1
        return last

    prepared = {name: tokenized(text) for name, text in fields.items()}
    size = len(target)
    for name, (words, tokens) in prepared.items():
        folded = [token for token, _ in tokens]
        for start in range(0, len(folded) - size + 1):
            if folded[start:start + size] == target:
                first, last = tokens[start][1], tokens[start + size - 1][1]
                last = with_trailing_symbols(words, last)
                return name, " ".join(words[first:last + 1])[:500], "exact"

    # Tolerance limitee (paraphrase legere du modele) : 80 % des mots porteurs
    # de la citation doivent apparaitre dans une fenetre courte de la source.
    wanted = {w for w in target if w not in _STOP}
    if len(wanted) < 4:
        return None
    best = None
    window_size = max(size + 4, 8)
    for name, (words, tokens) in prepared.items():
        for start in range(0, max(1, len(tokens) - 3)):
            window = tokens[start:start + window_size]
            overlap = len(wanted & {token for token, _ in window}) / len(wanted)
            if overlap >= min_overlap and (best is None or overlap > best[0]):
                first = window[0][1]
                last = with_trailing_symbols(words, window[-1][1])
                best = (overlap, name, " ".join(words[first:last + 1])[:500])
    if best:
        return best[1], best[2], "fuzzy"
    return None


def _grounded(text, reference, min_share):
    tokens = [t for t in _content_tokens(text) if len(t) > 2]
    if not tokens:
        return False
    reference_tokens = set(_content_tokens(reference))
    hits = sum(
        1 for t in tokens
        if t in reference_tokens or any(r.startswith(t) or t.startswith(r)
                                        for r in reference_tokens if len(r) > 3 and len(t) > 3)
    )
    return hits / len(tokens) >= min_share


# ---------------------------------------------------------------------------
# Validation deterministe
# ---------------------------------------------------------------------------

def source_fields(conn, payload):
    """
    Champs de preuve SEPARES : un extrait du resume IA (description_ai) n'a pas
    la meme valeur qu'un extrait de la video (titre, description originale,
    transcript). Le champ exact est garde dans la provenance.
    """
    fields = {"title": payload.get("title")}
    row = None
    if payload.get("content_id") is not None:
        row = conn.execute(
            "SELECT description_original, description_ai, description_manual "
            "FROM contents WHERE id = ?",
            (payload["content_id"],),
        ).fetchone()
    if row:
        fields["description_original"] = row[0]
        fields["description_manual"] = row[2]
        fields["description_ai"] = row[1]
    else:
        fields["description"] = payload.get("description")
    fields["transcript"] = payload.get("transcript")
    return fields


# Extrait issu d'un resume genere par IA : preuve de seconde main.
SECOND_HAND_FIELDS = {"description_ai"}


def validate_claim(item, payload, entities=None, fields=None):
    """
    Retourne (claim_valide | None, raison_de_rejet | None).
    Un claim valide peut etre 'accepted' ou 'needs_review'.
    """
    if not isinstance(item, dict):
        return None, "not_object"
    claim_text = analysis._clean_text(item.get("claim_text"), max_len=600)
    evidence = analysis._clean_text(item.get("evidence"), max_len=600)
    if not claim_text:
        return None, "missing_claim_text"
    if not evidence:
        return None, "missing_evidence"

    fields = fields or {
        "title": payload.get("title"),
        "description": payload.get("description"),
        "transcript": payload.get("transcript"),
    }
    located = locate_evidence(evidence, fields)
    if located is None:
        return None, "evidence_not_found_in_source"
    evidence_field, excerpt, match = located

    reasons = []
    subject = analysis._clean_text(item.get("subject"), max_len=200) or ""
    predicate = analysis._clean_text(item.get("predicate"), max_len=120) or ""
    object_value = analysis._clean_text(item.get("object"), max_len=300)

    # Valeur chiffree : elle doit figurer dans l'extrait (sinon inventee).
    number, unit = parse_number(object_value)
    if number is not None and number not in evidence_numbers(excerpt):
        return None, "object_number_not_in_evidence"

    if not subject or not predicate:
        reasons.append("missing_structure")
    elif not _grounded(subject, f"{excerpt} {payload.get('title') or ''}", 0.5):
        reasons.append("subject_not_grounded")
    if object_value:
        words = re.sub(r"\d[\d ,.]*", " ", object_value)
        # Avec un chiffre, chaque mot porteur doit etre dans l'extrait :
        # "80 000 likes" ne doit pas devenir "80 000 utilisateurs".
        share = 1.0 if number is not None else 0.5
        if _content_tokens(words) and not _grounded(words, excerpt, share):
            reasons.append("object_not_grounded")

    # Type d'affirmation : jamais promu en "fait" ; reclasse si l'extrait
    # porte des marqueurs d'opinion, conseil, prediction...
    proposed_kind = str(item.get("kind") or "").strip().lower()
    cue_kinds = detect_kinds(f"{excerpt} {claim_text}")
    if proposed_kind not in CLAIM_KINDS:
        reasons.append("kind_unspecified")
        kind = cue_kinds[0] if cue_kinds else "fact"
    elif proposed_kind == "fact" and cue_kinds:
        kind = cue_kinds[0]
        reasons.append(f"kind_downgraded_from_fact_to_{kind}")
    else:
        kind = proposed_kind

    # Modalite : jamais plus forte que ce que dit l'extrait.
    evidence_modality = detect_modality(excerpt)
    proposed_modality = str(item.get("modality") or "").strip().lower()
    if proposed_modality not in MODALITIES:
        proposed_modality = evidence_modality
    modality = min(
        (proposed_modality, evidence_modality),
        key=lambda value: _MODALITY_STRENGTH[value],
    )
    ok, strength_reason = analysis._claim_preserves_evidence_strength(claim_text, excerpt)
    if not ok:
        if strength_reason in ("unsupported_requirement", "unsupported_guarantee"):
            return None, strength_reason
        reasons.append(strength_reason)

    polarity = str(item.get("polarity") or "affirmative").strip().lower()
    if polarity not in ("affirmative", "negative"):
        polarity = "affirmative"

    evidence_type = str(item.get("evidence_type") or "").strip().lower()
    cue_type = detect_evidence_type(excerpt)
    if evidence_type not in EVIDENCE_TYPES:
        evidence_type = cue_type or "unspecified"
    elif evidence_type == "study" and cue_type != "study":
        # "etude" non mentionnee dans l'extrait : pas de preuve d'etude.
        evidence_type = cue_type or "unspecified"
        reasons.append("study_not_in_evidence")

    scope = analysis._clean_text(item.get("scope"), max_len=200)
    if scope and _fold(scope) in _NULL_SCOPES:
        scope = None
    if scope and not _grounded(scope, f"{excerpt} {payload.get('transcript') or ''}", 0.6):
        scope = None
        reasons.append("scope_not_grounded_dropped")
    scope = scope or detect_scope(excerpt)

    temporal_ref, temporal_year = detect_temporal(excerpt)
    sensitivity = detect_sensitivity(f"{claim_text} {excerpt}", payload.get("category"))

    try:
        confidence = float(item.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    if match == "fuzzy":
        confidence *= 0.9
    if evidence_field in SECOND_HAND_FIELDS:
        confidence *= 0.9
        reasons.append("evidence_from_ai_summary")
    if modality in ("possible", "conditional"):
        confidence = min(confidence, 0.5)
    if kind in ("opinion", "prediction"):
        confidence = min(confidence, 0.6)
    if evidence_type in ("promotional", "testimony"):
        confidence = min(confidence, 0.6)
    if sensitivity:
        confidence = min(confidence, 0.5)
        reasons.append(f"sensitive_{sensitivity}")
    if confidence < ACCEPT_MIN_CONFIDENCE:
        reasons.append("low_confidence")

    subject_entity_id = None
    for entity in entities or payload.get("entities") or []:
        if subject and _fold(entity["name"]) and _fold(entity["name"]) in _fold(subject):
            subject_entity_id = entity["id"]
            break

    # Les requalifications et remarques d'ancrage partiel restent tracees,
    # mais seules les raisons bloquantes imposent la review.
    informative = {"scope_not_grounded_dropped", "evidence_from_ai_summary"}
    blocking = [
        r for r in reasons
        if not r.startswith("kind_downgraded") and r not in informative
    ]
    return {
        "claim_text": claim_text,
        "subject": subject or claim_text[:80],
        "subject_key": normalize_key(subject or claim_text[:80]),
        "subject_entity_id": subject_entity_id,
        "predicate": predicate or "unspecified",
        "predicate_key": predicate_key(predicate) if predicate else "unspecified",
        "object_value": object_value,
        "object_key": normalize_key(object_value) if object_value else None,
        "object_number": number,
        "object_unit": unit,
        "polarity": polarity,
        "claim_kind": kind,
        "modality": modality,
        "confidence": round(confidence, 3),
        "evidence_type": evidence_type,
        "evidence_excerpt": excerpt,
        "evidence_field": evidence_field,
        "evidence_match": match,
        "scope": scope,
        "scope_key": normalize_key(scope) if scope else None,
        "temporal_ref": temporal_ref,
        "temporal_year": temporal_year,
        "sensitivity": sensitivity,
        "validation_status": "needs_review" if blocking else "accepted",
        "review_reasons": reasons,
    }, None


# ---------------------------------------------------------------------------
# Extracteurs (le modele propose, le validateur decide)
# ---------------------------------------------------------------------------

CLAIM_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "claim_text": {"type": "string"},
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "polarity": {"type": "string", "enum": ["affirmative", "negative"]},
                    "kind": {"type": "string", "enum": list(CLAIM_KINDS)},
                    "modality": {"type": "string", "enum": list(MODALITIES)},
                    "confidence": {"type": "number"},
                    "evidence_type": {"type": "string", "enum": list(EVIDENCE_TYPES)},
                    "evidence": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "required": ["claim_text", "subject", "predicate", "object", "kind",
                             "modality", "confidence", "evidence"],
            },
        }
    },
    "required": ["claims"],
}


def build_prompt(payload):
    return f"""
Extrait les affirmations VERIFIABLES de la source ci-dessous. N'utilise aucune
connaissance externe. Au maximum 5 affirmations, les plus importantes.
Reste bref : chaque champ en quelques mots, la citation en 25 mots maximum.

Pour chaque affirmation :
- claim_text : paraphrase courte et fidele de ce que dit la source ;
- subject / predicate / object : qui/quoi, quelle relation, quelle valeur ;
- polarity : affirmative ou negative ;
- kind : fact (fait verifiable), hypothesis, advice (conseil), opinion,
  prediction, personal_experience (temoignage de l'auteur) ;
  un avis ("le meilleur", "incroyable") est une opinion, jamais un fait ;
- modality : asserted, probable, possible, conditional (garde les "peut",
  "environ", "si" de la source) ;
- confidence : 0 a 1, ta confiance que la source dit bien cela ;
- evidence_type : study, measurement, demonstration, testimony,
  expert_statement, promotional, unspecified ;
- evidence : CITATION EXACTE copiee de la source (quelques mots a une phrase) ;
- scope : population ou perimetre SEULEMENT s'il est dit (ex. "chez les rats").

N'invente aucun chiffre, nom ou relation absent du texte. Si rien n'est
verifiable, renvoie une liste vide. JSON uniquement.

SOURCE
Titre : {payload.get('title') or ''}
Description : {payload.get('description') or ''}
Transcription : {payload.get('transcript') or ''}
""".strip()


class OllamaClaimExtractor:
    """Qwen local (qwen3:1.7b, num_ctx 4096 comme le Mentor : pas de rechargement)."""

    name = "ollama"

    def __init__(self, model=None, base_url=None, timeout=None, max_chars=None):
        self.model = model or os.environ.get("VAULT_CLAIMS_MODEL", "qwen3:1.7b")
        self.base_url = (base_url or os.environ.get("VAULT_OLLAMA_URL")
                         or "http://localhost:11434").rstrip("/")
        self.timeout = float(timeout or os.environ.get("VAULT_CLAIMS_TIMEOUT", "240"))
        self.max_chars = int(max_chars or os.environ.get("VAULT_CLAIMS_MAX_CHARS", "3500"))

    def extract(self, payload):
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": CLAIM_JSON_SCHEMA,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 1000},
            "messages": [
                {"role": "system", "content": (
                    "Tu extrais des affirmations structurees d'un texte fourni, "
                    "sans connaissance externe. JSON uniquement.")},
                {"role": "user", "content": build_prompt(payload)},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.load(response)
            content = raw["message"]["content"]
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ClaimExtractionError(f"Ollama indisponible : {type(exc).__name__}") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise ClaimExtractionError("reponse invalide du modele local") from exc
        return parse_claims_json(content)


def parse_claims_json(content):
    """
    JSON {"claims": [...]} ; si la sortie a ete tronquee (limite de tokens),
    recupere les objets claim COMPLETS deja emis et ignore le reste.
    Les objets recuperes passent ensuite par le meme validateur strict.
    """
    text = str(content or "")
    try:
        data = json.loads(text)
        claims = data.get("claims") if isinstance(data, dict) else None
        if isinstance(claims, list):
            return claims
    except ValueError:
        pass
    start = text.find("[", text.find('"claims"'))
    if start < 0:
        raise ClaimExtractionError("reponse JSON invalide du modele local")
    decoder = json.JSONDecoder()
    position, claims = start + 1, []
    while True:
        while position < len(text) and text[position] in " \r\n\t,":
            position += 1
        if position >= len(text) or text[position] != "{":
            break
        try:
            item, position = decoder.raw_decode(text, position)
        except ValueError:
            break  # objet coupe par la limite de tokens : ignore
        claims.append(item)
    if not claims:
        raise ClaimExtractionError("reponse JSON invalide du modele local")
    return claims


class CloudClaimExtractor:
    """Extracteur via vault_cloud (optionnel, seulement si une cle existe)."""

    name = "cloud"

    def __init__(self, provider=None, max_chars=6000):
        import vault_cloud

        self.provider = provider or vault_cloud.get_provider()
        if self.provider is None:
            raise ClaimExtractionError("aucun provider cloud configure")
        self.model = f"{self.provider.name}:{self.provider.model}"
        self.max_chars = max_chars

    def extract(self, payload):
        import vault_cloud

        try:
            response = self.provider.complete(
                "Tu extrais des affirmations structurees d'un texte fourni, sans "
                "connaissance externe. Reponds par un unique objet JSON "
                '{"claims": [...]} et rien d\'autre.',
                build_prompt(payload),
                max_tokens=4096,
                timeout=120,
            )
        except vault_cloud.CloudError as exc:
            raise ClaimExtractionError(str(exc)) from None
        text = response.text.strip()
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start:end + 1])
        except ValueError as exc:
            raise ClaimExtractionError("reponse JSON invalide du provider cloud") from exc
        claims = data.get("claims")
        if not isinstance(claims, list):
            raise ClaimExtractionError("champ claims absent")
        return claims


class StaticClaimExtractor:
    """Extracteur fixe (tests, sources synthetiques)."""

    name = "static"

    def __init__(self, claims_by_source, model="static-test"):
        self.claims_by_source = claims_by_source
        self.model = model
        self.max_chars = 30000

    def extract(self, payload):
        claims = self.claims_by_source.get(payload["source_id"])
        if isinstance(claims, Exception):
            raise claims
        return list(claims or [])


def extractor_from_name(name):
    name = (name or "ollama").strip().lower()
    if name in ("ollama", "local", "qwen"):
        return OllamaClaimExtractor()
    if name == "cloud":
        return CloudClaimExtractor()
    raise ClaimExtractionError(f"extracteur inconnu : {name!r}")


# ---------------------------------------------------------------------------
# Ecriture
# ---------------------------------------------------------------------------

def _fingerprint(payload, extractor):
    raw = json.dumps(
        {
            "version": EXTRACTION_VERSION,
            "extractor": extractor.name,
            "model": getattr(extractor, "model", None),
            "title": payload.get("title"),
            "description": payload.get("description"),
            "transcript": payload.get("transcript"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _observed_at(conn, source_id):
    row = conn.execute(
        "SELECT captured_at, created_at FROM intelligence_sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    if not row:
        return None
    return row[0] or row[1]


def extract_source(conn, source_id, extractor, *, dry_run=False, force=False):
    """
    Extrait, valide et ecrit les claims d'une source.
    dry_run=True : aucune ecriture, retourne ce qui serait ecrit.
    """
    ensure_schema(conn)
    payload = analysis.source_payload(
        conn, source_id, max_chars=getattr(extractor, "max_chars", None),
    )
    fingerprint = _fingerprint(payload, extractor)
    if not force and not dry_run:
        done = conn.execute(
            """
            SELECT id FROM intelligence_claim_extractions
             WHERE source_id = ? AND fingerprint = ? AND version = ?
               AND status = 'completed' AND superseded_at IS NULL
            """,
            (source_id, fingerprint, EXTRACTION_VERSION),
        ).fetchone()
        if done:
            return {"source_id": source_id, "status": "cached", "extraction_id": done[0]}

    started = time.perf_counter()
    try:
        raw_items = extractor.extract(payload)
    except ClaimExtractionError as exc:
        elapsed = round(time.perf_counter() - started, 1)
        if not dry_run:
            conn.execute(
                """
                INSERT INTO intelligence_claim_extractions(
                    source_id, fingerprint, extractor, model, version, status,
                    error, elapsed_s, created_at
                ) VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, ?)
                """,
                (source_id, fingerprint, extractor.name, getattr(extractor, "model", None),
                 EXTRACTION_VERSION, str(exc)[:500], elapsed, intelligence.now()),
            )
            conn.commit()
        return {"source_id": source_id, "status": "failed", "error": str(exc)[:300]}

    valid, rejected = [], []
    seen_texts = set()
    fields = source_fields(conn, payload)
    for item in raw_items:
        claim, reason = validate_claim(item, payload, fields=fields)
        if claim is None:
            rejected.append({"reason": reason, "item": item})
            continue
        key = _fold(claim["claim_text"])
        if key in seen_texts:
            rejected.append({"reason": "duplicate_in_extraction", "item": item})
            continue
        seen_texts.add(key)
        valid.append(claim)
    elapsed = round(time.perf_counter() - started, 1)

    result = {
        "source_id": source_id,
        "status": "dry_run" if dry_run else "completed",
        "claims": valid,
        "rejected": rejected,
        "elapsed_s": elapsed,
    }
    if dry_run:
        return result

    stamp = intelligence.now()
    observed_at = _observed_at(conn, source_id)
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            """
            INSERT INTO intelligence_claim_extractions(
                source_id, fingerprint, extractor, model, version, status,
                claims_written, rejected, raw_result, elapsed_s, created_at
            ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?)
            """,
            (source_id, fingerprint, extractor.name, getattr(extractor, "model", None),
             EXTRACTION_VERSION, len(valid),
             json.dumps(rejected, ensure_ascii=False),
             json.dumps(raw_items, ensure_ascii=False), elapsed, stamp),
        )
        extraction_id = cur.lastrowid

        # Les claims d'une extraction precedente de cette source sont
        # retires du graphe actif (jamais supprimes), sauf validation humaine.
        conn.execute(
            """
            UPDATE intelligence_claim_extractions SET superseded_at = ?
             WHERE source_id = ? AND id <> ? AND superseded_at IS NULL
            """,
            (stamp, source_id, extraction_id),
        )
        conn.execute(
            """
            UPDATE intelligence_claim_structure SET superseded_at = ?, updated_at = ?
             WHERE source_id = ? AND superseded_at IS NULL AND user_validated = 0
            """,
            (stamp, stamp, source_id),
        )

        written = []
        for claim in valid:
            provenance = {
                "extractor": extractor.name,
                "model": getattr(extractor, "model", None),
                "extraction_version": EXTRACTION_VERSION,
                "extraction_id": extraction_id,
                "source_fingerprint": fingerprint,
                "extracted_at": stamp,
                "source_url": payload.get("source_url"),
            }
            verification = (
                "asserted"
                if claim["claim_kind"] == "fact" and not claim["sensitivity"]
                else "unverified"
            )
            cur = conn.execute(
                """
                INSERT INTO intelligence_claims(
                    entity_id, source_id, claim_text, verification_status,
                    verification_source_id, notes, verified_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (claim["subject_entity_id"], source_id, claim["claim_text"], verification,
                 json.dumps({"structured": True, **provenance}, ensure_ascii=False,
                            sort_keys=True),
                 stamp, stamp),
            )
            claim_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO intelligence_claim_structure(
                    claim_id, source_id, content_id, extraction_id, subject, subject_key,
                    subject_entity_id, predicate, predicate_key, object_value, object_key,
                    object_number, object_unit, polarity, claim_kind, modality, confidence,
                    evidence_type, evidence_excerpt, evidence_field, evidence_match, scope,
                    scope_key, temporal_ref, temporal_year, observed_at, sensitivity,
                    validation_status, review_reasons, provenance, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (claim_id, source_id, payload.get("content_id"), extraction_id,
                 claim["subject"], claim["subject_key"], claim["subject_entity_id"],
                 claim["predicate"], claim["predicate_key"], claim["object_value"],
                 claim["object_key"], claim["object_number"], claim["object_unit"],
                 claim["polarity"], claim["claim_kind"], claim["modality"],
                 claim["confidence"], claim["evidence_type"], claim["evidence_excerpt"],
                 claim["evidence_field"], claim["evidence_match"], claim["scope"],
                 claim["scope_key"], claim["temporal_ref"], claim["temporal_year"],
                 observed_at, claim["sensitivity"], claim["validation_status"],
                 json.dumps(claim["review_reasons"], ensure_ascii=False),
                 json.dumps(provenance, ensure_ascii=False, sort_keys=True),
                 stamp, stamp),
            )
            written.append(claim_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result["extraction_id"] = extraction_id
    result["claim_ids"] = written
    return result


def pending_source_ids(conn, extractor, limit):
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT s.id FROM intelligence_sources s
          JOIN contents c ON c.id = s.content_id
         ORDER BY s.id
        """
    ).fetchall()
    selected = []
    for (source_id,) in rows:
        try:
            payload = analysis.source_payload(
                conn, source_id, max_chars=getattr(extractor, "max_chars", None),
            )
        except analysis.AnalysisError:
            continue
        fingerprint = _fingerprint(payload, extractor)
        done = conn.execute(
            """
            SELECT 1 FROM intelligence_claim_extractions
             WHERE source_id = ? AND fingerprint = ? AND version = ?
               AND status = 'completed' AND superseded_at IS NULL
            """,
            (source_id, fingerprint, EXTRACTION_VERSION),
        ).fetchone()
        if not done:
            selected.append(source_id)
        if len(selected) >= limit:
            break
    return selected


def claims_for_source(conn, source_id):
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT c.id, c.claim_text, c.verification_status, s.*
          FROM intelligence_claim_structure s
          JOIN intelligence_claims c ON c.id = s.claim_id
         WHERE s.source_id = ? AND s.superseded_at IS NULL
         ORDER BY c.id
        """,
        (source_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def main():
    parser = argparse.ArgumentParser(description="Claims structures avec provenance (V2)")
    parser.add_argument("--db", required=True)
    parser.add_argument("--extractor", default=os.environ.get("VAULT_CLAIMS_EXTRACTOR", "ollama"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-id", type=int)
    group.add_argument("--next", action="store_true")
    group.add_argument("--show", type=int, metavar="SOURCE_ID")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if args.show is not None:
            result = claims_for_source(conn, args.show)
        else:
            extractor = extractor_from_name(args.extractor)
            ids = [args.source_id] if args.source_id else pending_source_ids(
                conn, extractor, args.limit)
            result = []
            for index, source_id in enumerate(ids, 1):
                print(f"[{index}/{len(ids)}] source {source_id}...", file=sys.stderr, flush=True)
                result.append(extract_source(conn, source_id, extractor,
                                             dry_run=args.dry_run, force=args.force))
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
