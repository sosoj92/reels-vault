"""Outils communs : PowerShell, faux LIS, fausses bases. Jamais le vrai Vault."""

import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from _v2_fixtures import add_source, make_db

REPO = Path(__file__).resolve().parents[2]
REAL_DB = (REPO / "vault.db").resolve()
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
PYTHON = sys.executable

FAKE_TOKEN = "987654321:AAHsecretTokenValueForLisTests_abcdefghij"
FAKE_PASSPHRASE = "phrase de test tres longue 42"


def run_ps(script, *args, env=None, timeout=600):
    """Lance un script PowerShell du repo ; renvoie CompletedProcess (texte UTF-8)."""
    full_env = dict(os.environ)
    full_env.pop("LIS_BACKUP_PASSPHRASE", None)
    full_env.update(env or {})
    command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(REPO / script), *[str(a) for a in args]]
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=full_env, timeout=timeout)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def remove_tree(path):
    """Supprime un dossier de test : objets Git en lecture seule et chemins > 260 caracteres."""
    def _writable_then_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    extended = ("\\\\?\\" + str(path)) if os.name == "nt" else str(path)
    try:
        shutil.rmtree(extended, onexc=_writable_then_retry)
    except OSError:
        pass


def tmpdir(test, prefix="lis-deploy-test-"):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    test.addCleanup(remove_tree, path)
    return path


def fake_db(path, sources=3):
    path = Path(path)
    assert path.resolve() != REAL_DB, "jamais le vrai vault.db"
    conn = make_db(str(path))
    for i in range(sources):
        add_source(conn, f"https://example.test/video/{i}", f"Outil de recherche {i}",
                   transcript=f"un outil de recherche numero {i} pour tester le retrieval",
                   tags=("outils",))
    conn.close()
    return path


def count_contents(path):
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
    finally:
        conn.close()


def fake_lis(root, with_secrets=True):
    """Faux LIS minimal : lock, vault.db factice, fichiers personnels et faux secrets."""
    root = Path(root)
    (root / "deployment-kit").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "deployment-kit" / "lis.lock.json", root / "deployment-kit" / "lis.lock.json")
    shutil.copy2(REPO / ".env.example", root / ".env.example")
    fake_db(root / "vault.db")
    (root / "raw").mkdir()
    (root / "raw" / "fiche-test.md").write_text("# Fiche factice\n", encoding="utf-8")
    (root / "index.md").write_text("### Entree factice\n", encoding="utf-8")
    (root / "mobile-inbox").mkdir()
    (root / "mobile-inbox" / "inbox.txt").write_text("https://example.test/v/1\n", encoding="utf-8")
    jarvis = root / "external" / "jarvis-assistant-vocal"
    (jarvis / "notes").mkdir(parents=True)
    (jarvis / "notes" / "note.txt").write_text("note factice\n", encoding="utf-8")
    if with_secrets:
        (root / ".env").write_text(
            f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_ALLOWED_USER_ID=111\n"
            "OLLAMA_MODEL=qwen3:1.7b\nLLM_SENSITIVE_CLOUD_ALLOWED=false\n", encoding="utf-8")
        (jarvis / "config.yaml").write_text("api_key: jarvis-secret-value-xyz\n", encoding="utf-8")
    return root


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def rows(report):
    return {row["Component"]: row for row in report["results"]}


def git(*args, cwd=None):
    result = subprocess.run(["git", "-c", "user.name=LIS Test", "-c", "user.email=test@example.test",
                             "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *args],
                            cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} : {result.stderr}")
    return result.stdout.strip()
