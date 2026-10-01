#!/usr/bin/env python3
"""
install_jarvis_voice_bridge.py - Installe l'adaptateur Life Intelligence dans le
checkout local de sosoj92/jarvis-assistant-vocal.

Le checkout upstream est recreable (setup_upstreams.ps1, SHA du lock file).
Ce script reapplique donc uniquement notre petite integration locale :
1. copie les modules integrations/jarvis/*.py dans tools/ (life_intelligence,
   mentor_calendar, calendar_state_sync, replanning_*, dynamic_planner, weekly_horizon) ;
2. ajoute le domaine de routage vocal life_intelligence de facon idempotente ;
3. ajoute au brief Jarvis le resume Mentor / agenda / replanning, de facon idempotente.

Un fichier n'est reecrit que si son contenu change (fin de ligne et BOM ignores).
Aucun secret ni config utilisateur n'est modifie.

Usage :
    python install_jarvis_voice_bridge.py           # installe / reapplique
    python install_jarvis_voice_bridge.py --check   # lecture seule, code 0 si installe
"""

import argparse
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
JARVIS = ROOT / "external" / "jarvis-assistant-vocal"
INTEGRATIONS = ROOT / "integrations" / "jarvis"
SOURCE_TOOL = INTEGRATIONS / "life_intelligence.py"

MARKER = "# LIFE_INTELLIGENCE_BRIDGE"
PRIORITY_MARKER = "# LIFE_INTELLIGENCE_PRIORITY_ROUTE"
BRIEF_MARKER = "# LIFE_INTELLIGENCE_BRIEF"
# Hook du brief deja present (installe a la main avant ce script) : ne pas le dupliquer.
BRIEF_LEGACY_SIGNATURE = "from tools.mentor_calendar import _brief_text"
ROUTE_BLOCK = f'''    {MARKER}
    (("agent", "agents", "vault", "life intelligence", "knowledge graph",
      "data engineer", "multi agent", "multi-agent"),
     {{"life_intelligence"}}),
'''
PRIORITY_ANCHOR = '''    # Caméra/gestes : commandes locales explicites, avant toute interprétation
'''
PRIORITY_BLOCK = '''    # LIFE_INTELLIGENCE_PRIORITY_ROUTE
    try:
        from tools.life_intelligence import router_demande_agents
        route = router_demande_agents(phrase)
        if route:
            nom, arguments = route
            return Decision("outil", nom, arguments or {})
    except Exception:
        LOG.exception("routage Life Intelligence")

'''
ROUTE_ANCHOR = '''    (("hermes", "recherche de fond", "analyse approfondie", "compare",
      "comparaison", "synthese", "veille"), {"deleguer_a_hermes", "web"}),
'''
BRIEF_ANCHOR = '''    if _mail_configure():
'''
BRIEF_BLOCK = '''    # LIFE_INTELLIGENCE_BRIEF
    try:
        from tools.mentor_calendar import _brief_text
        mentor = _brief_text(14)
        if mentor:
            morceaux.append(mentor)
    except Exception:
        pass

    try:
        from tools.calendar_state_sync import inspect_changes, apply_changes
        calendar_changes = inspect_changes()

        if calendar_changes:
            apply_changes(calendar_changes)
    except Exception:
        pass

    try:
        from tools.replanning_watch import detect_replanning
        issues = detect_replanning(14)

        if issues:
            lignes = [
                f"Replanning : {len(issues)} écart(s) détecté(s)."
            ]

            for issue in issues[:3]:
                lignes.append(
                    f"{issue['title']} : {issue['message']}"
                )

            morceaux.append(" ".join(lignes))
    except Exception:
        pass
'''


class InstallError(RuntimeError):
    pass


def _normalized(path):
    return path.read_bytes().decode("utf-8", "replace").lstrip("﻿").replace("\r\n", "\n")


def _paths(jarvis):
    jarvis = Path(jarvis)
    return jarvis, jarvis / "core" / "routage_intentions.py", jarvis / "tools" / "brief.py"


def integration_modules():
    return sorted(p for p in INTEGRATIONS.glob("*.py") if p.name != "__init__.py")


def _patched_router(text):
    if PRIORITY_MARKER not in text:
        if PRIORITY_ANCHOR not in text:
            raise InstallError(
                "Point d'insertion prioritaire Jarvis introuvable dans core/routage_intentions.py"
            )
        text = text.replace(PRIORITY_ANCHOR, PRIORITY_BLOCK + PRIORITY_ANCHOR, 1)
    if MARKER not in text:
        if ROUTE_ANCHOR not in text:
            raise InstallError(
                "Point d'insertion Jarvis introuvable dans core/routage_intentions.py"
            )
        text = text.replace(ROUTE_ANCHOR, ROUTE_BLOCK + ROUTE_ANCHOR, 1)
    return text


def _brief_installed(text):
    return BRIEF_MARKER in text or BRIEF_LEGACY_SIGNATURE in text


def _patched_brief(text):
    if _brief_installed(text):
        return text
    if BRIEF_ANCHOR not in text:
        raise InstallError("Point d'insertion du brief introuvable dans tools/brief.py")
    return text.replace(BRIEF_ANCHOR, BRIEF_BLOCK + BRIEF_ANCHOR, 1)


def _write_if_changed(path, new_text):
    old = path.read_text(encoding="utf-8")
    if old == new_text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def check(jarvis=JARVIS):
    """Lecture seule : etat de l'integration dans le checkout Jarvis."""
    jarvis, router, brief = _paths(jarvis)
    state = {"jarvis": str(jarvis), "present": jarvis.exists(), "modules": {},
             "router": "absent", "brief": "absent"}
    if not jarvis.exists():
        state["installed"] = False
        return state
    for source in integration_modules():
        target = jarvis / "tools" / source.name
        if not target.exists():
            state["modules"][source.name] = "missing"
        elif _normalized(target) != _normalized(source):
            state["modules"][source.name] = "outdated"
        else:
            state["modules"][source.name] = "ok"
    if router.exists():
        text = router.read_text(encoding="utf-8")
        state["router"] = "ok" if (MARKER in text and PRIORITY_MARKER in text) else "missing"
    if brief.exists():
        state["brief"] = "ok" if _brief_installed(brief.read_text(encoding="utf-8")) else "missing"
    state["installed"] = (all(v == "ok" for v in state["modules"].values())
                          and state["router"] == "ok" and state["brief"] == "ok")
    return state


def install(jarvis=JARVIS):
    jarvis, router, brief = _paths(jarvis)
    if not jarvis.exists():
        raise InstallError(
            f"Jarvis upstream absent: {jarvis}. Lance setup_upstreams.ps1."
        )
    if not SOURCE_TOOL.exists():
        raise InstallError(f"Bridge tool source absent: {SOURCE_TOOL}")
    if not router.exists():
        raise InstallError(f"Jarvis router absent: {router}")

    # Tous les correctifs sont calcules avant la moindre ecriture.
    router_text = _patched_router(router.read_text(encoding="utf-8"))
    brief_text = None
    if brief.exists():
        brief_text = _patched_brief(brief.read_text(encoding="utf-8"))

    changed = []
    (jarvis / "tools").mkdir(parents=True, exist_ok=True)
    for source in integration_modules():
        target = jarvis / "tools" / source.name
        if not target.exists() or _normalized(target) != _normalized(source):
            shutil.copy2(source, target)
            changed.append(f"tools/{source.name}")
    if _write_if_changed(router, router_text):
        changed.append("core/routage_intentions.py")
    if brief_text is not None and _write_if_changed(brief, brief_text):
        changed.append("tools/brief.py")

    return {
        "jarvis": str(jarvis),
        "tool": str(jarvis / "tools" / SOURCE_TOOL.name),
        "router": str(router),
        "changed": changed,
        "status": "installed",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pont vocal Jarvis -> Life Intelligence")
    parser.add_argument("--check", action="store_true", help="lecture seule")
    parser.add_argument("--jarvis", default=str(JARVIS), help="checkout Jarvis")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.check:
        state = check(args.jarvis)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state["installed"] else 1
    print(json.dumps(install(args.jarvis), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
