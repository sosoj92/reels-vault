#!/usr/bin/env python3
"""
install_jarvis_voice_bridge.py - Installe l'adaptateur Life Intelligence dans le
checkout local de sosoj92/jarvis-assistant-vocal.

Le checkout upstream est recreable et peut etre reset a chaque synchronisation.
Ce script reapplique donc uniquement notre petite integration locale :
1. copie tools/life_intelligence.py ;
2. ajoute le domaine de routage vocal life_intelligence de facon idempotente.

Aucun secret ni config utilisateur n'est modifie.
"""

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent
JARVIS = ROOT / "external" / "jarvis-assistant-vocal"
SOURCE_TOOL = ROOT / "integrations" / "jarvis" / "life_intelligence.py"
TARGET_TOOL = JARVIS / "tools" / "life_intelligence.py"
ROUTER = JARVIS / "core" / "routage_intentions.py"

MARKER = "# LIFE_INTELLIGENCE_BRIDGE"
ROUTE_BLOCK = f'''    {MARKER}
    (("agent", "agents", "vault", "life intelligence", "knowledge graph",
      "data engineer", "multi agent", "multi-agent"),
     {{"life_intelligence"}}),
'''


class InstallError(RuntimeError):
    pass


def install():
    if not JARVIS.exists():
        raise InstallError(
            f"Jarvis upstream absent: {JARVIS}. Lance setup_upstreams.ps1."
        )
    if not SOURCE_TOOL.exists():
        raise InstallError(f"Bridge tool source absent: {SOURCE_TOOL}")
    if not ROUTER.exists():
        raise InstallError(f"Jarvis router absent: {ROUTER}")

    TARGET_TOOL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE_TOOL, TARGET_TOOL)

    text = ROUTER.read_text(encoding="utf-8")
    if MARKER not in text:
        anchor = '''    (("hermes", "recherche de fond", "analyse approfondie", "compare",
      "comparaison", "synthese", "veille"), {"deleguer_a_hermes", "web"}),
'''
        if anchor not in text:
            raise InstallError(
                "Point d'insertion Jarvis introuvable dans core/routage_intentions.py"
            )
        text = text.replace(anchor, ROUTE_BLOCK + anchor, 1)
        ROUTER.write_text(text, encoding="utf-8")

    return {
        "jarvis": str(JARVIS),
        "tool": str(TARGET_TOOL),
        "router": str(ROUTER),
        "status": "installed",
    }


def main():
    import json

    print(json.dumps(install(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
