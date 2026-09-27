#!/usr/bin/env python3
"""
filtre.py — choisit QUOI ingérer avant de lancer ingest.py.

Le problème : un export Instagram contient tout ce que tu as sauvegardé, mélangé —
des astuces utiles, des mèmes, des citations, des postes promotionnels. Passer les
400 liens à `ingest.py` (téléchargement + transcription Whisper) coûte des heures
pour des fiches que personne ne relira.

Ce script lit l'export, applique les règles de `filtres.yaml` (un fichier que TU
édites), et écrit la liste des liens retenus — que `ingest.py` consomme ensuite.

    python filtre.py --export "C:/Users/toi/Downloads/instagram-votre_compte-2026-01-01"
    python filtre.py --export ... --rapport          # que le rapport, n'écrit rien
    python filtre.py --export ... --liste-collections
    python ingest.py liens-filtres.txt

Deux niveaux de règles (voir l'en-tête de filtres.yaml) :
  1. `collections` — tes collections telles qu'elles existent dans ton compte, avec
     `garder: true/false`. L'export Instagram relie bien chaque post à sa collection :
     le filtre s'en sert donc en priorité, et le verdict par collection est exact.
     Astuce : le lien n'est pas dans une clé « media » (toujours vide) mais dans une
     entrée `label_values` sans libellé — c'est ce que ce script va chercher.
  2. `themes` — filet de sécurité pour les posts qu'aucune collection ne couvre :
     mots-clés (hashtag exact, ou mot entier de la légende). Un post qu'aucune règle
     ne classe suit `defaut`.

Aucune dépendance en dehors de PyYAML (pip install pyyaml).
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

FICHIER_CONFIG = Path(__file__).with_name("filtres.yaml")


# ---------------------------------------------------------------- lecture de l'export

def reparer_encodage(valeur):
    """L'export Instagram double-encode parfois l'UTF-8 : 'ð\\x9f…' -> emoji."""
    if not isinstance(valeur, str):
        return valeur or ""
    try:
        return valeur.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return valeur


def champs(entree):
    """Transforme une entrée d'export en dictionnaire {label: valeur}."""
    return {l.get("label"): l.get("value")
            for l in (entree.get("label_values") or []) if isinstance(l, dict)}


def chemin_export(dossier):
    """Trouve saved_posts.json / saved_collections.json, même imbriqué dans l'archive."""
    dossier = Path(dossier)
    if dossier.is_file():
        return dossier.parent, dossier
    for nom in ("saved_posts.json", "posts.json"):
        for candidat in sorted(dossier.rglob(nom)):
            return candidat.parent, candidat
    return dossier, dossier / "saved_posts.json"


def charge_posts(chemin):
    """Posts sauvegardés : URL, légende, hashtags, auteur."""
    donnees = json.loads(Path(chemin).read_text(encoding="utf-8", errors="ignore"))
    if isinstance(donnees, dict):
        donnees = donnees.get("saved_posts") or donnees.get("posts") or []

    posts, vus = [], set()
    for entree in donnees:
        if not isinstance(entree, dict):
            continue
        if entree.get("label_values"):
            champs_entree = champs(entree)
            url = champs_entree.get("URL") or ""
            legende = reparer_encodage(champs_entree.get("Légende")
                                       or champs_entree.get("LÃ©gende") or "")
            auteur = ""
            for l in (entree.get("label_values") or []):
                for niveau1 in (l.get("dict") or []):
                    for niveau2 in (niveau1.get("dict") or []):
                        if niveau2.get("label") in ("Nom de profil", "Nom du profil"):
                            auteur = niveau2.get("value", "")
        else:                                   # export simplifié / liste maison
            url = entree.get("url") or entree.get("link") or ""
            legende = entree.get("caption") or entree.get("description") or ""
            auteur = entree.get("owner") or entree.get("username") or ""
        url = str(url).strip()
        if not url or url in vus:
            continue
        vus.add(url)
        legende = reparer_encodage(legende)
        posts.append({
            "url": url,
            "legende": legende,
            "hashtags": [t.lower() for t in re.findall(r"#(\w+)", legende)],
            "auteur": str(auteur).lower(),
            "texte": legende.lower(),
        })
    return posts


def medias_collection(entree):
    """URLs des posts rangés dans une collection.

    Piège de l'export Instagram : la liste des posts n'est NI dans `media` (toujours
    vide) NI dans une clé nommée, mais dans une entrée `label_values` sans libellé,
    dont chaque `dict` contient un sous-`dict` avec un champ « URL ». C'est là, et
    nulle part ailleurs, qu'on trouve le lien post -> collection.
    """
    urls = []
    for l in (entree.get("label_values") or []):
        for media in (l.get("dict") or []):
            for champ in (media.get("dict") or []):
                if champ.get("label") == "URL" and champ.get("value"):
                    urls.append(str(champ["value"]))
        for lien in (l.get("media") or []):
            if isinstance(lien, str):
                urls.append(lien)
    for lien in (entree.get("media") or []):
        if isinstance(lien, str):
            urls.append(lien)
    return urls


def charge_collections(chemin):
    """Collections : nom, date, et les posts qu'elles contiennent (mapping exact)."""
    chemin = Path(chemin)
    if not chemin.exists():
        return [], {}
    donnees = json.loads(chemin.read_text(encoding="utf-8", errors="ignore"))
    if isinstance(donnees, dict):
        donnees = donnees.get("saved_collections") or []
    collections, index = [], {}
    for entree in donnees:
        if not isinstance(entree, dict):
            continue
        champs_entree = champs(entree)
        nom = reparer_encodage(champs_entree.get("Nom") or champs_entree.get("Name") or "")
        urls = medias_collection(entree)
        collections.append({"nom": nom,
                            "date": champs_entree.get("Heure de mise à jour")
                                    or champs_entree.get("Date", ""),
                            "medias": urls})
        for lien in urls:
            index[lien.split("?")[0]] = nom
    return collections, index


# ---------------------------------------------------------------- règles

def charge_config(chemin):
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML manquant : pip install pyyaml")
    if not Path(chemin).exists():
        sys.exit(f"fichier de règles introuvable : {chemin}")
    regles = yaml.safe_load(Path(chemin).read_text(encoding="utf-8")) or {}
    # Une cle presente mais laissee vide dans le YAML vaut None (et non la valeur
    # par defaut) : `themes:` sans contenu suffisait a faire planter le rapport.
    # On normalise donc par TYPE, pas par presence.
    for cle, defaut in (("themes", {}), ("collections", []), ("exclusions", [])):
        if not isinstance(regles.get(cle), type(defaut)):
            regles[cle] = defaut
    if not isinstance(regles.get("defaut"), str) or not regles["defaut"].strip():
        regles["defaut"] = "revoir"
    return regles


def correspond(post, mot):
    """Mot-clé présent ? Hashtag à l'identique, ou mot entier dans la légende.

    Piège : un test de sous-chaîne fait matcher « ai » dans « frais » ou « airbnb »
    (des centaines de faux positifs). Les mots-clés courts ne sont donc acceptés
    que comme hashtags exacts.
    """
    mot = str(mot).lower()
    if mot in post["hashtags"]:
        return True
    if len(mot) >= 5:
        return re.search(rf"(?<![\w-]){re.escape(mot)}(?![\w-])", post["texte"]) is not None
    return False


def normalise_nom(nom):
    """Clé de comparaison tolérante : « 💡| Astuces » == « Astuces »."""
    return re.sub(r"[^0-9a-z]+", "", str(nom).lower())


def table_collections(regles):
    """nom de collection (normalisé) -> garder (bool)."""
    table = {}
    for entree in regles["collections"]:
        if isinstance(entree, dict) and entree.get("nom"):
            table[normalise_nom(entree["nom"])] = bool(entree.get("garder", True))
        elif isinstance(entree, str):
            table[normalise_nom(entree)] = True
    return table


def classer(post, regles, table_collections_, index_collections):
    """Rend (verdict, motif) avec verdict dans garder / ecarter / revoir."""
    if any(correspond(post, mot) for mot in regles["exclusions"]):
        return "ecarter", "exclusion"

    # 1. Mapping exact post -> collection (priorité absolue : c'est le classement
    #    de l'utilisateur, pas une déduction par mots-clés).
    collection = index_collections.get(post["url"].split("?")[0])
    if collection:
        if table_collections_.get(normalise_nom(collection), True):
            return "garder", f"collection: {collection}"
        return "ecarter", f"collection: {collection}"

    # 2. Mots-clés : on écarte d'abord (le rejet est prioritaire), puis on garde.
    for theme, regle in regles["themes"].items():
        if not isinstance(regle, dict):
            continue
        mots = regle.get("mots_cles") or []
        if regle.get("garder", True) is False and any(correspond(post, m) for m in mots):
            return "ecarter", f"thème: {theme}"
    for theme, regle in regles["themes"].items():
        if not isinstance(regle, dict):
            continue
        if regle.get("garder", True) is False:
            continue
        if any(correspond(post, m) for m in (regle.get("mots_cles") or [])):
            return "garder", f"thème: {theme}"

    return "revoir", "non classé"


def ecrit_config_initiale(collections, chemin):
    """Écrit un filtres.yaml de départ, pré-rempli avec les collections de l'export."""
    entete = """# filtres.yaml — ce qui entre dans ta base, et ce qui n'y entre pas.
#
# Généré depuis TON export : les noms de collections sont les tiens, pas des exemples.
# Tout est en `garder: true` au départ — passe à `false` ce qui ne t'intéresse pas
# (mèmes, citations, contenu promotionnel...), puis vérifie avec :
#     python filtre.py --export "CHEMIN/VERS/TON_EXPORT" --rapport
#
# `themes` est un filet de sécurité pour les posts qu'aucune collection ne couvre :
# des mots-clés (hashtag exact, ou mot entier de la légende). Un thème en
# `garder: false` rejette ; le rejet gagne toujours.
#
# `defaut` : verdict des posts qu'aucune règle ne classe.
#    "revoir"  -> ni ingérés ni perdus, listés dans le rapport
#    "garder"  -> tout est ingéré
#    "ecarter" -> ignorés définitivement
defaut: revoir

# Mots-clés qui écartent un post quoi qu'il arrive.
exclusions: []

collections:
"""
    lignes = [entete]
    for entree in sorted(collections, key=lambda c: str(c["nom"]).lower()):
        nom = json.dumps(entree["nom"], ensure_ascii=False)
        lignes.append(f"  - nom: {nom}\n    garder: true\n")
    lignes.append("""
themes:
  # Exemple : décommente et adapte. Les mots-clés courts ne comptent que comme
  # hashtags exacts (sinon « ai » matcherait « frais » ou « airbnb »).
  # astuces:
  #   garder: true
  #   mots_cles: [astuce, astuces, tips, tuto, lifehack]
  # memes:
  #   garder: false
  #   mots_cles: [meme, memes, humour, drole]
""")
    Path(chemin).write_text("".join(lignes), encoding="utf-8")


# ---------------------------------------------------------------- programme

def main():
    parseur = argparse.ArgumentParser(
        description="Filtre les posts sauvegardés d'un export Instagram avant ingestion.")
    parseur.add_argument("--export", required=True,
                         help="dossier (ou fichier) de l'export Instagram")
    parseur.add_argument("--config", default=str(FICHIER_CONFIG),
                         help="fichier de règles (défaut : filtres.yaml)")
    parseur.add_argument("--sortie", default="liens-filtres.txt",
                         help="où écrire les liens retenus")
    parseur.add_argument("--journal", default=None,
                         help="journal.json du coffre, pour ne pas refaire l'existant")
    parseur.add_argument("--liste-collections", action="store_true",
                         help="affiche les collections de l'export et le verdict, puis sort")
    parseur.add_argument("--init-config", action="store_true",
                         help="écrit un filtres.yaml de départ depuis les collections de l'export")
    parseur.add_argument("--force", action="store_true",
                         help="autorise --init-config à écraser un fichier existant")
    parseur.add_argument("--rapport", action="store_true",
                         help="rapport seulement : n'écrit aucun fichier de liens")
    parseur.add_argument("--exemples", type=int, default=2,
                         help="exemples de légende affichés par thème (0 = aucun)")
    parseur.add_argument("--limite", type=int, default=0,
                         help="ne retenir que les N premiers liens")
    args = parseur.parse_args()

    dossier, chemin_posts = chemin_export(args.export)
    chemin_collections = dossier / "saved_collections.json"

    if args.init_config:
        # Avant tout chargement de règles : le fichier n'existe pas encore.
        collections_export, _ = charge_collections(chemin_collections)
        if not collections_export:
            sys.exit(f"aucune collection lisible dans {chemin_collections}")
        chemin = Path(args.config)
        if chemin.exists() and not args.force:
            sys.exit(f"{chemin} existe déjà (--force pour l'écraser)")
        ecrit_config_initiale(collections_export, chemin)
        print(f"{chemin} écrit : {len(collections_export)} collections, toutes `garder: true`.")
        print("Ouvre-le, passe à false celles qui ne t'intéressent pas, puis :")
        print(f"  python filtre.py --export \"{args.export}\" --rapport")
        return 0

    regles = charge_config(args.config)
    collections, index_collections = charge_collections(chemin_collections)
    table = table_collections(regles)

    if args.liste_collections:
        print(f"{len(collections)} collections dans {chemin_collections.name}")
        print(f"posts reliés à une collection par l'export : "
              f"{len(index_collections)} URL(s) uniques\n")
        for entree in sorted(collections, key=lambda c: str(c["nom"]).lower()):
            verdict = "GARDER " if table.get(normalise_nom(entree["nom"]), True) else "ÉCARTER"
            print(f"  [{verdict}] {len(entree['medias']):4d} post(s)  {entree['nom']}")
        manquantes = [nom for nom, _ in
                      [(e.get("nom"), e.get("garder")) for e in regles["collections"]
                       if isinstance(e, dict)]
                      if normalise_nom(nom) not in
                      {normalise_nom(c['nom']) for c in collections}]
        if manquantes:
            print("\n  dans filtres.yaml mais absentes de l'export :")
            for nom in manquantes:
                print(f"    - {nom}")
        return 0

    if args.init_config:
        chemin = Path(args.config)
        if chemin.exists() and not args.force:
            sys.exit(f"{chemin} existe déjà (--force pour l'écraser)")
        ecrit_config_initiale(collections, chemin)
        print(f"{chemin} écrit : {len(collections)} collections, toutes `garder: true`.")
        print("Ouvre-le, passe à false celles qui ne t'intéressent pas, puis :")
        print(f"  python filtre.py --export \"{args.export}\" --rapport")
        return 0

    posts = charge_posts(chemin_posts)
    if not posts:
        sys.exit(f"aucun post lisible dans {chemin_posts}")

    journal = {}
    if args.journal and Path(args.journal).exists():
        journal = json.loads(Path(args.journal).read_text(encoding="utf-8"))

    par_motif, exemples = Counter(), defaultdict(list)
    retenus, deja = [], 0
    for post in posts:
        verdict, motif = classer(post, regles, table, index_collections)
        par_motif[f"{verdict}: {motif}"] += 1
        if verdict != "garder":
            continue
        if len(exemples[motif]) < args.exemples:
            exemples[motif].append((post["legende"][:90].replace("\n", " ")
                                    or post["url"]))
        if journal.get(post["url"], {}).get("statut") == "ok":
            deja += 1
            continue
        retenus.append(post["url"])

    if args.limite:
        retenus = retenus[:args.limite]

    print(f"posts sauvegardés dans l'export : {len(posts)}")
    print(f"déjà ingérés (journal)          : {deja}")
    print(f"à ingérer après filtre          : {len(retenus)}")
    print(f"verdict par défaut (`defaut`)   : {regles['defaut']}\n")
    print("--- détail ---")
    for motif, nombre in par_motif.most_common():
        print(f"  {nombre:5d}  {motif}")
        for exemple in exemples.get(motif.split(": ", 1)[-1], []):
            print(f"           ex. {exemple}")

    if regles["defaut"] == "garder":
        non_classes = [m for m in par_motif if m.startswith("revoir")]
        if non_classes:
            print("\n  `defaut: garder` est actif : les posts non classés partent à l'ingestion.")

    if not args.rapport:
        Path(args.sortie).write_text("\n".join(retenus) + ("\n" if retenus else ""),
                                     encoding="utf-8")
        print(f"\nliste écrite : {args.sortie} ({len(retenus)} liens)")
        print(f"puis : python ingest.py {args.sortie}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
