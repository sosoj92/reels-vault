"""Pont vocal Jarvis -> Life Intelligence / Agency Agents.

Fichier maintenu dans Roseytech/reels-vault (integrations/jarvis/) puis copie
dans external/jarvis-assistant-vocal/tools/ par install_jarvis_voice_bridge.py
(appele par setup_upstreams.ps1).

Lecture seule : cet outil ne modifie rien et n'execute aucune action externe.

V1 RETRIEVAL-FIRST : l'outil lance ``vault_voice_bridge.py --ask`` avec le
Python du Vault. Le Vault cherche d'abord les vraies sources sauvegardees et
renvoie une reponse deterministe citee (titres + URLs). Une synthese Qwen
locale (qwen3:1.7b) est ajoutee seulement si elle arrive avant l'echeance ;
sinon la reponse deterministe part seule. Aucun long appel LLM obligatoire.
"""

import os
import re
import subprocess
import unicodedata
from pathlib import Path

from core.config import reglage
from core.registre import outil


# Echeance de la synthese Qwen cote Vault (la reponse citee ne l'attend pas
# si Ollama est absent). Qwen 1.7B a chaud : ~12-15 s sur cette machine.
_LLM_TIMEOUT_DEFAUT = 25
# Marge pour le demarrage Python + recherche (< 1 s en pratique).
_MARGE_PROCESSUS = 20


def _vault_root() -> Path:
    cfg = (reglage("integrations.life_intelligence_vault", "") or "").strip()
    if cfg:
        return Path(cfg).expanduser().resolve()
    return (Path.home() / "reels-vault").resolve()


def _normaliser(phrase: str) -> str:
    texte = unicodedata.normalize("NFKD", str(phrase or "").lower())
    texte = "".join(ch for ch in texte if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texte).split())


# Mention explicite du Vault / des agents -> consultation des agents.
_EXPLICITE = (
    "mon vault",
    "dans le vault",
    "mes agents",
    "consulte mes agents",
    "parle a mes agents",
    "demande a mes agents",
    "life intelligence",
    "ask life intelligence",
    "knowledge graph engineer",
    "data engineer",
    "multi agent systems architect",
    "demande au knowledge graph",
    "parle au knowledge graph",
)

# Formulations sans ambiguite : c'est bien le contenu sauvegarde qui est vise.
_RAPPEL_SANS_AMBIGUITE = (
    "dans mes sauvegardes",
    "dans mes sources",
    "dans mes videos",
    "dans mes tiktoks",
    "dans mes reels",
    "mes sauvegardes sur",
    "mes sources sur",
    "mes videos sur",
    "mes tiktoks sur",
    "mes reels sur",
)

# "j'avais sauvegarde", "j'ai deja enregistre", "j'avais trouve"...
_VERBE_RAPPEL = re.compile(
    r"\bj (?:avais|ai) (?:\w+ )?"
    r"(?:sauvegard|enregistr|trouv|gard|vu|not|mis de cote)"
)

# Intention de recherche : question ou demande de rappel.
_INTENTION = re.compile(
    r"\b(?:qu est ce que|quoi|quel|quels|quelle|quelles|lesquel\w*|"
    r"retrouve|retrouver|rappelle|souviens|montre|liste|dis moi|"
    r"cherche|recherche|est ce que)\b"
)

# Le sujet ou le contexte de sauvegarde, pour ne pas capturer
# "j'ai trouve mes cles" ou "j'ai garde le recu".
_SUJET = re.compile(
    r"\b(?:sur|a propos|concernant|au sujet)\b"
    r"|\bmes (?:videos|sources|tiktoks|reels|sauvegardes|favoris)\b"
)


def router_demande_agents(phrase: str):
    """
    Route deterministe vers Life Intelligence (sinon None : Jarvis garde la main).

    - mention explicite du Vault / agents / Life Intelligence
      -> consulter_agents_vault
    - rappel personnel : "qu'est-ce que j'avais sauvegarde sur X ?",
      "retrouve ce que j'avais trouve sur X"
      -> ask_life_intelligence
    """
    raw = str(phrase or "").strip()
    p = _normaliser(raw)
    if not p:
        return None

    if any(expr in p for expr in _EXPLICITE):
        return ("consulter_agents_vault", {"question": raw})

    if any(expr in p for expr in _RAPPEL_SANS_AMBIGUITE):
        return ("ask_life_intelligence", {"question": raw})

    intention = bool(_INTENTION.search(p)) or raw.rstrip().endswith("?")
    if _VERBE_RAPPEL.search(p) and intention and _SUJET.search(p):
        return ("ask_life_intelligence", {"question": raw})

    return None


def _llm_timeout() -> float:
    try:
        valeur = float(
            reglage("integrations.life_intelligence_llm_timeout", _LLM_TIMEOUT_DEFAUT)
        )
    except (TypeError, ValueError):
        valeur = _LLM_TIMEOUT_DEFAUT
    return max(0.0, min(valeur, 45.0))


def _llm_actif() -> bool:
    valeur = reglage("integrations.life_intelligence_llm", True)
    if isinstance(valeur, str):
        return valeur.strip().lower() not in ("0", "false", "non", "off", "no")
    return bool(valeur)


def _commande(question: str, agent: str = "", mode: str = "ask"):
    root = _vault_root()
    py = root / ".venv" / "Scripts" / "python.exe"
    bridge = root / "vault_voice_bridge.py"
    db = root / "vault.db"

    if not py.exists():
        raise RuntimeError(f"Python du Vault introuvable : {py}")
    if not bridge.exists():
        raise RuntimeError(f"Bridge vocal introuvable : {bridge}")
    if not db.exists():
        raise RuntimeError(f"Base Vault introuvable : {db}")

    cmd = [
        str(py),
        str(bridge),
        question,
        "--db",
        str(db),
        "--ask",
        "--mode",
        mode,
        "--llm-timeout",
        str(_llm_timeout()),
    ]
    if agent:
        cmd += ["--agent", agent]
    if not _llm_actif():
        cmd.append("--no-llm")
    return cmd


def _reponse_agentique(question: str, agent: str = "", mode: str = "ask") -> str:
    """Recherche retrieval-first dans le Vault ; synthese locale optionnelle."""
    try:
        cmd = _commande(question, agent, mode)
    except Exception as exc:
        return f"Life Intelligence indisponible : {str(exc)[:180]}"

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_vault_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_llm_timeout() + _MARGE_PROCESSUS,
            check=False,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            },
        )
    except subprocess.TimeoutExpired:
        return "Le pont Life Intelligence a mis trop de temps a repondre."
    except Exception as exc:
        return f"Life Intelligence indisponible : {str(exc)[:180]}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return "Life Intelligence a echoue : " + (detail[-1][:240] if detail else "erreur inconnue")

    texte = (proc.stdout or "").strip()
    if not texte:
        return "Life Intelligence n'a produit aucune reponse exploitable."

    try:
        from core.confidentialite import caviarder
    except Exception:
        return texte
    return caviarder(texte)


@outil(
    nom="consulter_agents_vault",
    description=(
        "Consulte les agents specialises du Life Intelligence System et le Vault "
        "personnel. Utiliser quand l'utilisateur parle de ses agents, du Vault, "
        "de Life Intelligence, d'un Knowledge Graph Engineer, Data Engineer, "
        "Multi-Agent Systems Architect, ou demande une analyse de ses contenus. "
        "L'outil choisit automatiquement les bons agents, sauf si un nom est precise. "
        "Lecture seule : aucune action externe."
    ),
    parametres={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "La demande complete de l'utilisateur."
            },
            "agent": {
                "type": "string",
                "description": "Nom exact d'un agent demande explicitement, sinon vide."
            },
        },
        "required": ["question"],
    },
    confirmation=False,
    lent=True,
    phrase_attente="Je consulte tes agents et ton Vault.",
    mcp_expose=False,
    affichage="toujours",
)
def consulter_agents_vault(question: str, agent: str = "") -> str:
    question = str(question or "").strip()
    if not question:
        return "Je n'ai pas recu de question pour tes agents."
    return _reponse_agentique(question, agent, mode="consult")


@outil(
    nom="ask_life_intelligence",
    description=(
        "Recherche et raisonne dans la memoire personnelle Life Intelligence "
        "et le Vault. Utiliser pour retrouver ce que l'utilisateur avait "
        "sauvegarde, trouve, appris ou conserve sur un sujet. "
        "Lecture seule : aucune action externe."
    ),
    parametres={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "Question de rappel ou de recherche "
                    "dans Life Intelligence."
                )
            },
            "agent": {
                "type": "string",
                "description": (
                    "Nom exact d'un agent specialise "
                    "si explicitement demande."
                )
            },
        },
        "required": ["question"],
    },
    confirmation=False,
    lent=True,
    phrase_attente="Je recherche dans ta memoire Life Intelligence.",
    mcp_expose=False,
    affichage="toujours",
)
def ask_life_intelligence(question: str, agent: str = "") -> str:
    question = str(question or "").strip()
    if not question:
        return (
            "Je n'ai pas recu de question "
            "a rechercher dans Life Intelligence."
        )
    return _reponse_agentique(question, agent, mode="ask")
