"""
v2_guard.py - Verrou des ecritures Life Intelligence V2 sur le VRAI Vault.

Tant que la V2 n'a pas recu la validation finale de l'utilisateur, aucune
ecriture propre a la V2 ne touche le vrai vault.db :
- index semantique, calibrage ;
- claims structures, evaluations (convergences, contradictions...) ;
- double analyse Agency du Mentor et sa trace de consensus ;
- revue cloud de la file de review ;
- tables et colonnes V2.

Les tables et colonnes V2 ne sont jamais creees sur le vrai Vault tant que
le verrou est ferme. Les lectures restent possibles (Ask peut utiliser un
index deja present).

Les bases temporaires, les copies jetables et les tests ne sont PAS
concernes : la V2 y fonctionne pleinement.

Deverrouillage (apres validation finale uniquement) : creer le fichier
`life_intelligence_v2.enabled` a la racine du Vault (non versionne), ou
definir VAULT_V2_WRITES=1.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REAL_DB = ROOT / "vault.db"
FLAG = ROOT / "life_intelligence_v2.enabled"


class V2WritesBlocked(RuntimeError):
    """Ecriture V2 refusee sur le vrai Vault (V2 non encore validee)."""


def db_path(conn):
    """Chemin du fichier de la base principale ('' pour :memory:)."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if tuple(row)[1] == "main":
            return tuple(row)[2] or ""
    return ""


def is_real_vault(path):
    if not path:
        return False
    try:
        return Path(path).resolve() == REAL_DB.resolve()
    except OSError:
        return False


def enabled():
    if os.environ.get("VAULT_V2_WRITES", "").strip().lower() in ("1", "on", "true", "oui"):
        return True
    return FLAG.exists()


def v2_writes_allowed(conn_or_path):
    path = conn_or_path if isinstance(conn_or_path, (str, Path)) else db_path(conn_or_path)
    return not is_real_vault(path) or enabled()


def require_v2_writes(conn_or_path, what):
    if not v2_writes_allowed(conn_or_path):
        raise V2WritesBlocked(
            f"{what} : ecriture V2 bloquee sur le vrai Vault tant que la V2 n'est "
            f"pas validee (fichier {FLAG.name} absent)."
        )
