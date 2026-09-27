"""Pont vocal Jarvis -> Life Intelligence / Agency Agents.

Fichier maintenu dans Roseytech/reels-vault puis copie automatiquement dans
external/jarvis-assistant-vocal/tools/ par setup_upstreams.ps1.

Lecture seule : cet outil ne modifie rien et n'execute aucune action externe.
Il lance le bridge du Vault avec le Python du Vault, puis renvoie au LLM Jarvis
le contexte borne + les playbooks compacts des agents selectionnes.
"""

import json
import os
import subprocess
from pathlib import Path

from core.config import reglage
from core.registre import outil


def _vault_root() -> Path:
    cfg = (reglage("integrations.life_intelligence_vault", "") or "").strip()
    if cfg:
        return Path(cfg).expanduser().resolve()
    return (Path.home() / "reels-vault").resolve()


def _commande(question: str, agent: str = ""):
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
    return cmd


def _formater(paquet: dict) -> str:
    agents = paquet.get("agents") or []
    noms = ", ".join(a.get("name", "?") for a in agents) or "aucun"

    blocs = [
        f"AGENTS SELECTIONNES: {noms}",
        "",
        "INSTRUCTION: reponds maintenant a la demande utilisateur en appliquant "
        "les playbooks ci-dessous. Pour toute affirmation propre au Vault, "
        "utilise uniquement le contexte Vault fourni. Si le Vault ne suffit pas, dis-le.",
    ]

    for a in agents:
        blocs += [
            "",
            f"--- {a.get('name', 'Agent')} ---",
            a.get("description", ""),
            a.get("playbook", ""),
        ]

    blocs += [
        "",
        "CONTEXTE VAULT:",
        json.dumps(paquet.get("vault_context"), ensure_ascii=False),
        "",
        "DEMANDE:",
        str(paquet.get("text") or ""),
    ]
    return "\n".join(blocs)


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

    try:
        proc = subprocess.run(
            _commande(question, agent),
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

    try:
        paquet = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "Life Intelligence a renvoye une reponse illisible."

    return _formater(paquet)
