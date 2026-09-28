#!/usr/bin/env python3
"""
migrate_to_db.py - Importe les donnees existantes du Vault dans vault.db.

Sources lues (jamais modifiees ni supprimees) :
    raw/*.md          fiches brutes : lien, auteur, description, transcription, images
    index.md          titre, categorie, sous-themes, tags, resume, ressources
    taxonomy.md       categories et sous-themes de reference, tags frequents
    journal.json      correspondance lien -> fiche, echecs d'ingestion

IDEMPOTENT : relancer le script ne cree aucun doublon. Le lien source
(source_url) est l'identifiant metier ; a defaut, le nom de la fiche raw/.
Les corrections faites dans vault.db (description_manual, champs verrouilles,
notes, actions, ressources deja presentes) ne sont jamais ecrasees.

Usage :
    python migrate_to_db.py
    python migrate_to_db.py --db autre.db      (pour tester sur une copie)
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import vault_db
from generate_vault import decouper, lire_index, lire_taxonomie

RACINE = Path(__file__).resolve().parent


def identifiant(lien):
    """Meme logique que ingest.py : derive le nom de fiche depuis l'URL."""
    fin = lien.rstrip("/").split("/")[-1].split("?")[0]
    fin = re.sub(r"[^A-Za-z0-9_-]", "", fin)[:40]
    plateforme = "tiktok" if "tiktok" in lien else "insta"
    return f"{plateforme}_{fin}"


def canonique(lien):
    """URL sans parametres de suivi (?_r=..., ?igsh=...) ni fragment."""
    lien = lien.strip()
    p = urlsplit(lien)
    if not p.scheme:
        return lien
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, "", ""))


def lien_de_fiche(source, nom):
    """Le champ source peut concatener plusieurs liens : garder celui qui correspond a la fiche."""
    liens = ["https://" + l for l in re.split(r"https?://", source) if l.strip()]
    for l in liens:
        if identifiant(l) == nom:
            return l
    return liens[0] if liens else source


def lire_fiche(chemin):
    texte = chemin.read_text(encoding="utf-8", errors="ignore")
    fiche = {"nom": chemin.stem, "fichier": chemin.name}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", texte, re.S)
    if m:
        for ligne in m.group(1).splitlines():
            if ":" in ligne:
                cle, _, val = ligne.partition(":")
                fiche[cle.strip()] = val.strip()
        texte = texte[m.end():]
    t = re.search(r"^# (.+)$", texte, re.M)
    fiche["titre"] = t.group(1).strip() if t else ""
    sections = {}
    for bloc in re.split(r"^## ", texte, flags=re.M)[1:]:
        entete, _, corps = bloc.partition("\n")
        sections[entete.strip().lower()] = corps.strip()
    vide = ("", "(vide)", "(pas d'audio exploitable)", "(aucune)")
    desc = sections.get("description", "")
    trans = sections.get("transcription audio", "")
    fiche["description"] = None if desc in vide else desc
    fiche["transcription"] = None if trans in vide else trans
    fiche["images"] = [l[2:].strip() for l in sections.get("images", "").splitlines() if l.startswith("- ")]
    return fiche


TYPES = {vault_db.fold(t): t for t in vault_db.RESOURCE_TYPES}


def ressources(texte):
    """
    'OpenAlternative [site], Codex [logiciel] (lu a l'ecran), Notion'
    -> [{name, type, description}]. Sans [type] reconnu : type 'autre' (jamais devine).
    """
    res = []
    for morceau in decouper(texte or ""):
        type_ = "autre"
        m = re.search(r"\s*\[([^\]]+)\]", morceau)
        if m and vault_db.fold(m.group(1).strip()) in TYPES:
            type_ = TYPES[vault_db.fold(m.group(1).strip())]
            morceau = (morceau[:m.start()] + morceau[m.end():]).strip()
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", morceau)
        nom, desc = (m.group(1), m.group(2)) if m and m.group(1).strip() else (morceau, None)
        nom = nom.strip().strip("«»\"' ").strip()
        if nom:
            res.append({"name": nom, "description": desc, "type": type_})
    return res


def entier(valeur):
    try:
        return int(float(valeur))
    except (TypeError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Importe raw/ + index.md dans vault.db (idempotent)")
    parser.add_argument("--vault", default=RACINE, type=Path)
    parser.add_argument("--db", default=None, type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    v = args.vault

    conn = vault_db.connect(args.db or v / "vault.db")

    # 1. Taxonomie de reference.
    categories, tags_frequents = lire_taxonomie(v / "taxonomy.md")
    for cat, sous in categories.items():
        for s in sous:
            vault_db.ensure_subtheme(conn, s, cat)
    for t in tags_frequents:
        vault_db._id_nom(conn, "tags", t)

    # 2. Sources.
    fiches = {f.stem: lire_fiche(f) for f in sorted((v / "raw").glob("*.md"))}
    index = {identifiant(e["lien"]): e for e in lire_index(v / "index.md") if e.get("lien")}
    journal = {}
    if (v / "journal.json").exists():
        journal = json.loads((v / "journal.json").read_text(encoding="utf-8"))
    lien_journal = {i["fiche"][:-3]: l for l, i in journal.items() if i.get("statut") == "ok" and i.get("fiche")}

    # 3. Import : chaque fiche raw/, enrichie par son entree d'index si elle existe.
    bilan = {"created": 0, "updated": 0, "unchanged": 0}
    for nom in sorted(set(fiches) | set(index)):
        f, e = fiches.get(nom, {}), index.get(nom, {})
        lien = e.get("lien") or lien_de_fiche(f.get("source") or lien_journal.get(nom, ""), nom)
        if not lien:
            print(f"  ! {nom} : aucun lien source, ignore")
            continue
        data = {
            "source_url": canonique(lien),
            "platform": e.get("plateforme") or f.get("plateforme"),
            "content_type": f.get("genre"),
            "author": e.get("auteur") or f.get("auteur"),
            "title": e.get("titre") or f.get("titre"),
            "description_original": f.get("description"),
            "description_ai": e.get("contenu") or None,
            "transcript": f.get("transcription"),
            "category": e.get("categorie") or None,
            "source_file": f.get("fichier"),
            "duration_s": entier(f.get("duree_s")),
            "ingested_at": f.get("traite_le"),
            "subthemes": e.get("sous_themes", []),
            "tags": e.get("tags", []),
            "resources": ressources(e.get("outils")),
            "media": f.get("images", []),
        }
        cid, etat = vault_db.upsert_content_from_import(conn, data)
        bilan[etat] += 1
        indexe = "indexee" if nom in index else "NON indexee (raw seule)"
        print(f"  [{etat:9}] #{cid} {nom} - {indexe}")
    conn.commit()

    echecs = [l for l, i in journal.items() if i.get("statut") != "ok"]
    total = conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
    print(f"\n{bilan['created']} cree(s), {bilan['updated']} mis a jour, {bilan['unchanged']} inchange(s)"
          f" - {total} contenu(s) dans vault.db")
    if echecs:
        print(f"{len(echecs)} lien(s) en echec dans journal.json (pas de fiche, non importes) :")
        for l in echecs:
            print(f"  - {l}")
    conn.close()


if __name__ == "__main__":
    main()
