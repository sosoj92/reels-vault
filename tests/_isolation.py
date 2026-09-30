"""
Isolation des tests : A IMPORTER EN PREMIER dans chaque fichier de test.

vault_db, vault_api et connectors lisent leurs chemins (base, images, exports) au
moment de leur import. Ce module fixe ces chemins vers un dossier temporaire AVANT
tout import, neutralise les vrais services (ClickUp...) et interdit tout acces reseau.
Ainsi aucun test ne peut lire ou modifier le vrai Vault, quel que soit l'ordre des tests.
"""

import os
import sys
import tempfile
import urllib.request
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
VRAIE_BASE = (RACINE / "vault.db").resolve()

if "vault_db" in sys.modules and Path(sys.modules["vault_db"].DB_PATH).resolve() == VRAIE_BASE:
    raise RuntimeError("tests/_isolation.py doit etre importe avant vault_db (risque d'ecrire dans le vrai Vault)")

TMP = Path(tempfile.mkdtemp(prefix="reels-vault-test-"))
os.environ["REELS_VAULT_DB"] = str(TMP / "vault.db")
os.environ["REELS_VAULT_IMAGES"] = str(TMP / "images")
os.environ["REELS_VAULT_EXPORTS"] = str(TMP / "exports")
os.environ["LLM_USAGE_DB"] = str(TMP / "llm_usage.db")
# Tests hors ligne : aucune cle cloud reelle n'est jamais utilisee.
for _k in ("MISTRAL_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[_k] = ""
os.environ["TASK_PROVIDER"] = ""  # present mais vide : ignore aussi la variable Windows
# Variable presente mais vide = service non configure (bloque aussi la lecture des variables Windows).
for _v in ("CLICKUP_API_TOKEN", "CLICKUP_LIST_ID"):
    os.environ[_v] = ""
sys.path.insert(0, str(RACINE))


def reseau_interdit(*a, **k):
    raise AssertionError("appel reseau interdit pendant les tests")


urllib.request.urlopen = reseau_interdit

# Aucune connexion reseau reelle (meme via http.client ou un SDK) : echec immediat,
# jamais d'attente de timeout si Internet est coupe.
import socket  # noqa: E402

_create_connection = socket.create_connection


def _socket_interdit(address, *args, **kwargs):
    raise OSError(f"connexion reseau interdite pendant les tests : {address[0]}")


socket.create_connection = _socket_interdit


def verifier():
    """A appeler dans setUp/setUpClass : echoue si un chemin pointe vers le vrai Vault."""
    import connectors.base
    import vault_db
    assert Path(vault_db.DB_PATH).resolve() != VRAIE_BASE, "les tests visent la VRAIE vault.db"
    assert Path(connectors.base.EXPORT_DIR).resolve() != (RACINE / "exports").resolve(), "exports reels vises"
