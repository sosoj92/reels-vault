#!/usr/bin/env python3
"""
ingest.py — transforme une liste de liens TikTok / Instagram en fiches Markdown.

Pour chaque vidéo :
  1. récupère les métadonnées (titre, description, auteur, hashtags)
  2. télécharge l'audio et le transcrit en local avec faster-whisper
  3. extrait 3 images de la vidéo (pour le texte incrusté à l'écran)
  4. écrit une fiche Markdown dans vault/raw/

Le script reprend là où il s'est arrêté : on peut le couper (Ctrl+C) et le
relancer sans retraiter ce qui est déjà fait.

Usage :
    python ingest.py mes_liens.txt
    python ingest.py saved_posts.json --cookies chrome
    python ingest.py liens.txt --limite 20        (pour tester sur 20 vidéos)
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- configuration

VAULT = Path.home() / "vault"          # modifiable avec --vault
MODELE_WHISPER = "small"               # tiny / base / small / medium
PAUSE_ENTRE_VIDEOS = 3                 # secondes, pour ne pas se faire bloquer
NB_IMAGES = 3

YTDLP = [sys.executable, "-m", "yt_dlp"]
GALLERYDL = [sys.executable, "-m", "gallery_dl"]

# Contournement TikTok : certains appels yt-dlp échouent sans User-Agent explicite.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)

MOTIF_LIEN = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?(?:tiktok\.com|instagram\.com)/[^\s\"'<>,\)\]]+"
)


# ---------------------------------------------------------------- utilitaires

def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def verifier_outils():
    """Verifie que yt-dlp et ffmpeg repondent avant de commencer."""
    manquants = []

    try:
        subprocess.run(YTDLP + ["--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        manquants.append("yt-dlp  ->  pip install yt-dlp")

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        manquants.append(
            "ffmpeg  ->  winget install Gyan.FFmpeg  (puis rouvrir PowerShell)"
        )

    if manquants:
        log("ERREUR : outil(s) manquant(s)")
        for m in manquants:
            log(f"   {m}")
        sys.exit(1)


def extraire_liens(chemin):
    """Récupère toutes les URLs TikTok/Instagram d'un fichier, quel que soit
    son format (txt, csv, json d'export). Les doublons sont supprimés."""
    texte = Path(chemin).read_text(encoding="utf-8", errors="ignore")
    liens, vus = [], set()
    for lien in MOTIF_LIEN.findall(texte):
        lien = lien.rstrip(".,;")
        if lien not in vus:
            vus.add(lien)
            liens.append(lien)
    return liens


def options_cookies(cookies):
    """--cookies accepte soit un nom de navigateur (firefox, edge...),
    soit le chemin d'un fichier cookies.txt exporte depuis le navigateur."""
    if not cookies:
        return []
    if Path(cookies).exists():
        return ["--cookies", str(Path(cookies).resolve())]
    return ["--cookies-from-browser", cookies]


def charger_journal(chemin):
    if chemin.exists():
        return json.loads(chemin.read_text(encoding="utf-8"))
    return {}


def sauver_journal(chemin, journal):
    chemin.write_text(
        json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def identifiant(lien):
    """Un nom de fichier court et sûr, dérivé de l'URL."""
    fin = lien.rstrip("/").split("/")[-1].split("?")[0]
    fin = re.sub(r"[^A-Za-z0-9_-]", "", fin)[:40]
    plateforme = "tiktok" if "tiktok" in lien else "insta"
    return f"{plateforme}_{fin or str(abs(hash(lien)))[:10]}"


def options_ytdlp(lien, cookies):
    """Options communes à yt-dlp.

    On force le User-Agent uniquement pour TikTok afin de ne pas modifier
    inutilement le comportement d'Instagram.
    """
    options = []
    if "tiktok.com" in lien:
        options += ["--user-agent", USER_AGENT]
    options += options_cookies(cookies)
    return options


# ---------------------------------------------------------------- étapes

def recuperer_metadonnees(lien, cookies):
    commande = YTDLP + ["--dump-json", "--no-warnings", "--skip-download"]
    commande += options_ytdlp(lien, cookies)
    commande.append(lien)

    resultat = subprocess.run(
        commande,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if resultat.returncode != 0:
        raise RuntimeError((resultat.stderr or "yt-dlp a échoué").strip()[:300])

    return json.loads(resultat.stdout.splitlines()[0])


def telecharger_media(lien, dossier, nom, cookies):
    """Télécharge l'audio (m4a) et la vidéo en basse qualité (pour les images)."""
    audio = dossier / f"{nom}.m4a"
    video = dossier / f"{nom}.mp4"

    base = YTDLP + ["--no-warnings", "--quiet"]
    base += options_ytdlp(lien, cookies)

    subprocess.run(
        base + [
            "-f", "bestaudio",
            "-x",
            "--audio-format", "m4a",
            "-o", str(dossier / f"{nom}.%(ext)s"),
            lien,
        ],
        capture_output=True,
        timeout=300,
    )

    subprocess.run(
        base + [
            "-f", "worstvideo[height>=480]/worst",
            "-o", str(video),
            lien,
        ],
        capture_output=True,
        timeout=300,
    )

    return (
        audio if audio.exists() else None,
        video if video.exists() else None,
    )


def transcrire(audio, modele):
    if audio is None:
        return ""
    segments, _ = modele.transcribe(str(audio), vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def telecharger_carrousel(lien, dossier_images, nom, cookies):
    """Post Instagram sans vidéo : on télécharge les images du carrousel.

    Sur un carrousel, ce sont les images QUI SONT le contenu (les slides
    "10 meilleurs restos de Lisbonne"). On récupère aussi la légende, que
    yt-dlp ne sait pas lire sur ce type de post.
    """
    cible = dossier_images / nom
    cible.mkdir(parents=True, exist_ok=True)

    commande = GALLERYDL + ["--write-metadata", "-D", str(cible)]
    commande += options_cookies(cookies)
    commande.append(lien)
    subprocess.run(commande, capture_output=True, timeout=300)

    images = sorted(
        p for p in cible.glob("*")
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    )

    legende = ""
    for fichier_meta in sorted(cible.glob("*.json")):
        try:
            donnees = json.loads(fichier_meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        legende = donnees.get("description") or donnees.get("caption") or ""
        if legende:
            break

    return images, legende


def extraire_images(video, dossier_images, nom, duree):
    """Prend NB_IMAGES captures réparties dans la vidéo."""
    if video is None or not duree:
        return []

    chemins = []
    for i in range(NB_IMAGES):
        instant = duree * (i + 1) / (NB_IMAGES + 1)
        sortie = dossier_images / f"{nom}_{i + 1}.jpg"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{instant:.1f}",
                "-i", str(video),
                "-frames:v", "1",
                "-vf", "scale=720:-1",
                str(sortie),
            ],
            capture_output=True,
            timeout=90,
        )
        if sortie.exists():
            chemins.append(sortie)

    return chemins


def ecrire_fiche(dossier, vault, nom, lien, meta, transcription, images, genre):
    liens_images = []
    for p in images:
        try:
            liens_images.append(f"- {p.relative_to(vault).as_posix()}")
        except ValueError:
            liens_images.append(f"- {p.name}")

    contenu = f"""---
source: {lien}
plateforme: {"TikTok" if "tiktok" in lien else "Instagram"}
genre: {genre}
auteur: {meta.get("uploader") or meta.get("channel") or "inconnu"}
duree_s: {meta.get("duration") or ""}
traite_le: {datetime.now():%Y-%m-%d}
statut: brut
---

# {(meta.get("title") or nom)[:120]}

## Description
{(meta.get("description") or "").strip() or "(vide)"}

## Transcription audio
{transcription or "(pas d'audio exploitable)"}

## Images
{chr(10).join(liens_images) or "(aucune)"}
"""
    (dossier / f"{nom}.md").write_text(contenu, encoding="utf-8")


# ---------------------------------------------------------------- programme

def main():
    parseur = argparse.ArgumentParser()
    parseur.add_argument("fichier", help="fichier contenant les liens")
    parseur.add_argument("--vault", default=str(VAULT))
    parseur.add_argument(
        "--cookies",
        default=None,
        help="navigateur pour les cookies (chrome, firefox, edge)",
    )
    parseur.add_argument(
        "--limite",
        type=int,
        default=0,
        help="ne traiter que les N premières vidéos",
    )
    parseur.add_argument("--modele", default=MODELE_WHISPER)
    args = parseur.parse_args()

    verifier_outils()

    vault = Path(args.vault)
    dossier_raw = vault / "raw"
    dossier_images = vault / "images"
    dossier_temp = vault / ".temp"
    for d in (dossier_raw, dossier_images, dossier_temp):
        d.mkdir(parents=True, exist_ok=True)

    chemin_journal = vault / "journal.json"
    journal = charger_journal(chemin_journal)

    liens = extraire_liens(args.fichier)
    a_faire = [l for l in liens if journal.get(l, {}).get("statut") != "ok"]
    if args.limite:
        a_faire = a_faire[:args.limite]

    log(f"{len(liens)} liens trouvés, {len(a_faire)} à traiter.")
    if not a_faire:
        return

    log(f"Chargement du modèle Whisper « {args.modele} » (long la 1re fois)...")
    from faster_whisper import WhisperModel

    modele = WhisperModel(args.modele, device="cpu", compute_type="int8")

    reussites = echecs = 0
    for numero, lien in enumerate(a_faire, 1):
        nom = identifiant(lien)
        log(f"[{numero}/{len(a_faire)}] {lien}")

        try:
            erreur_meta = ""
            try:
                meta = recuperer_metadonnees(lien, args.cookies)
            except Exception as e:
                meta = {}  # carrousel, ou probleme d'acces
                erreur_meta = str(e).replace("\n", " ")[:300]

            audio = video = None
            if meta.get("duration"):
                genre = "video"
                audio, video = telecharger_media(
                    lien,
                    dossier_temp,
                    nom,
                    args.cookies,
                )
                transcription = transcrire(audio, modele)
                images = extraire_images(
                    video,
                    dossier_images,
                    nom,
                    meta.get("duration"),
                )
            else:
                genre = "carrousel"
                transcription = ""
                images, legende = telecharger_carrousel(
                    lien,
                    dossier_images,
                    nom,
                    args.cookies,
                )
                if not images:
                    raise RuntimeError(
                        "ni vidéo ni image récupérée | yt-dlp : "
                        + (erreur_meta or "aucun message")
                    )
                if legende and not meta.get("description"):
                    meta["description"] = legende

            ecrire_fiche(
                dossier_raw,
                vault,
                nom,
                lien,
                meta,
                transcription,
                images,
                genre,
            )

            for fichier in (audio, video):
                if fichier and fichier.exists():
                    fichier.unlink()

            journal[lien] = {"statut": "ok", "fiche": f"{nom}.md"}
            reussites += 1

        except Exception as erreur:  # noqa: BLE001 — on continue quoi qu'il arrive
            log(f"    échec : {erreur}")
            journal[lien] = {
                "statut": "echec",
                "erreur": str(erreur)[:300],
            }
            echecs += 1

        sauver_journal(chemin_journal, journal)
        time.sleep(PAUSE_ENTRE_VIDEOS)

    log(f"Terminé — {reussites} réussites, {echecs} échecs.")
    log(f"Fiches disponibles dans : {dossier_raw}")
    if echecs:
        log("Relance le script pour retenter uniquement les échecs.")


if __name__ == "__main__":
    main()
