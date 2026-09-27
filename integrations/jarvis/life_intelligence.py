"""Pont vocal Jarvis -> Life Intelligence / Agency Agents.

Fichier maintenu dans Roseytech/reels-vault puis copie automatiquement dans
external/jarvis-assistant-vocal/tools/ par setup_upstreams.ps1.

Lecture seule : cet outil ne modifie rien et n'execute aucune action externe.
Il lance le bridge du Vault avec le Python du Vault, puis renvoie au LLM Jarvis
le contexte borne + les playbooks compacts des agents selectionnes.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from core.config import reglage
from core.registre import outil


def _vault_root() -> Path:
    cfg = (reglage("integrations.life_intelligence_vault", "") or "").strip()
    if cfg:
        return Path(cfg).expanduser().resolve()
    return (Path.home() / "reels-vault").resolve()


def router_demande_agents(phrase: str):
    """Route deterministe : ne laisse pas un petit LLM decider si le Vault est vise."""
    p = " ".join(re.sub(r"[^a-z0-9]+", " ", str(phrase or "").lower()).split())
    expressions = (
        "mon vault",
        "dans le vault",
        "mes agents",
        "consulte mes agents",
        "parle a mes agents",
        "demande a mes agents",
        "life intelligence",
        "knowledge graph engineer",
        "data engineer",
        "multi agent systems architect",
        "demande au knowledge graph",
        "parle au knowledge graph",
    )
    if any(expr in p for expr in expressions):
        return "consulter_agents_vault", {"question": str(phrase or "").strip()}
    return None


def _commande(question: str, agent: str = "", prompt: bool = False):
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
        "--max-agents",
        "2",
    ]
    if agent:
        cmd += ["--agent", agent]
    if prompt:
        cmd.append("--prompt")
    return cmd


def _extraire_texte_reponse(rep) -> str:
    morceaux = []
    for bloc in getattr(rep, "content", []) or []:
        if getattr(bloc, "type", None) == "text" and getattr(bloc, "text", None):
            morceaux.append(bloc.text)
    return " ".join(morceaux).strip()


def _reponse_agentique(question: str, agent: str = "") -> str:
    """Construit le prompt Vault/agents puis fait UNE reponse sans tool-calling."""
    try:
        proc = subprocess.run(
            _commande(question, agent, prompt=True),
            cwd=str(_vault_root()),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        return "Le pont Life Intelligence a mis trop de temps a repondre."
    except Exception as exc:
        return f"Life Intelligence indisponible : {str(exc)[:180]}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return f"Life Intelligence a echoue : {detail[:240]}"

    prompt = (proc.stdout or "").strip()
    if not prompt:
        return "Life Intelligence n'a produit aucun contexte exploitable."

    try:
        from core.llm import llm
        fournisseur = llm()
        if not fournisseur.disponible():
            return "Le modele Jarvis n'est pas disponible."
        rep = fournisseur.repondre(
            prompt,
            [{"role": "user", "content": question}],
            [],
        )
        texte = _extraire_texte_reponse(rep)
        if not texte:
            return "Je n'ai pas obtenu de reponse exploitable de l'agent."
        from core.confidentialite import caviarder
        return caviarder(texte)
    except Exception as exc:
        return f"L'agent n'a pas reussi a repondre : {str(exc)[:180]}"


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
    return _reponse_agentique(question, agent)
