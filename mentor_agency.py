import json
import os
import sys
import urllib.request
import time

import vault_orchestrator as agency_router


OLLAMA_URL = (
    os.environ.get("VAULT_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    + "/api/chat"
)
MODEL = "qwen3:1.7b"


SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": [
                "opportunity",
                "convergence",
                "novelty",
                "low_signal"
            ]
        },
        "relevance": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
        },
        "needs_cloud": {
            "type": "boolean"
        }
    },
    "required": [
        "classification",
        "relevance",
        "confidence",
        "needs_cloud"
    ]
}


def analyze(
    text,
    *,
    qwen_result=None,
    heuristic_outcome=None,
    similarity=0.0,
):
    try:
        context = agency_router.build_orchestration_context(
            text[:5000],
            max_agents=3
        )
    except Exception as exc:
        # Playbooks Agency absents ou illisibles : pas d'arbitre Agency.
        # Le worker envoie alors la source en review au lieu de planter.
        print(
            "[mentor_agency ROUTING ERROR]",
            type(exc).__name__,
            str(exc)[:200],
            file=sys.stderr,
        )
        return None

    agents = context.get("agents", [])

    if not agents:
        return None

    # V1 fast mode:
    # Agency routing chooses the specialists.
    # The LLM receives their roles, not tens of thousands of chars
    # of playbooks.
    agent_briefs = [
        {
            "name": a["name"],
            "description": a["description"][:500],
        }
        for a in agents
    ]

    prompt = f"""
Life Intelligence Mentor Engine.

Analyse la source comme les spécialistes suivants :
{json.dumps(agent_briefs, ensure_ascii=False)}

Analyse rapide précédente :
{json.dumps(qwen_result, ensure_ascii=False)}

Heuristique précédente :
{heuristic_outcome}

Similarité Vault :
{similarity:.3f}

SOURCE :
{text[:5000]}

Décide uniquement :
- opportunity = directement exploitable
- convergence = confirme une information déjà connue
- novelty = nouvelle information utile mais non immédiatement exploitable
- low_signal = bruit, générique, insuffisant ou peu utile

IMPORTANT :
Une technologie intéressante n'est pas automatiquement une opportunité.
needs_cloud=true seulement si tu ne peux réellement pas trancher
avec une confiance suffisante ou si une vérification externe est indispensable.
""".strip()

    payload = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "keep_alive": "30m",
        "options": {
            "temperature": 0,
            "num_predict": 90,
            "num_ctx": 4096
        },
        "messages": [
            {
                "role": "system",
                "content": (
                    "Tu es un comité d'experts. "
                    "Réponds uniquement avec le JSON demandé. "
                    "Sois conservateur sur la pertinence et l'escalade cloud."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    }

    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(
            req,
            timeout=60
        ) as response:
            raw = json.load(response)

        result = json.loads(
            raw["message"]["content"]
        )

        result["agents"] = [
            a["name"]
            for a in agents
        ]

        result["routes"] = context["routes"]

        return result

    except Exception as exc:
        # stderr : le stdout du worker est un JSON exploitable.
        print(
            "[mentor_agency ERROR]",
            type(exc).__name__,
            repr(exc),
            file=sys.stderr,
        )
        return None
