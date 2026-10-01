#!/usr/bin/env python3
r"""
lis_deploy.py - Outils de deploiement du LIS (bibliotheque standard uniquement).

Utilise par CHECK_LIS.ps1, backup_lis.ps1 et restore_lis.ps1. Ne depend d'aucun
paquet tiers : il fonctionne avec n'importe quel Python 3.12+ (y compris avant la
creation de la .venv, pendant une restauration).

Regles :
- aucune ecriture dans une base source : ouverture `mode=ro` ou `query_only` ;
- aucune valeur de secret n'est jamais affichee (seulement CONFIGURED / NOT CONFIGURED) ;
- sorties JSON sur stdout, sans contenu personnel (comptes et etats seulement).

Commandes :
    sqlite-backup --src vault.db --dst backup/vault.db
    integrity     --db vault.db [--tables a,b,c]
    vault-health  --root . --db vault.db [--semantic]
    env-status    --env .env --keys K1,K2
    secret-scan   --root .
    tree-copy     --src DIR --dst DIR [--exclude a,b] [--no-overwrite]
    manifest-files  --dir BACKUP
    manifest-verify --dir BACKUP [--versions 1]
    remove-partial  --dir BACKUP.partial

Les chemins sont manipules en forme etendue Windows (prefixe \\?\) : les noms de fichiers
tres longs (images/ des videos, > 260 caracteres) sont copies et verifies meme si
LongPathsEnabled est desactive.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

EXTENDED_PREFIX = "\\\\?\\"


def _out(payload, code=0):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return code


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ext(path):
    r"""Chemin absolu en forme etendue Windows (\\?\) : pas de limite MAX_PATH."""
    path = os.path.abspath(str(path))
    if os.name != "nt" or path.startswith(EXTENDED_PREFIX):
        return path
    if path.startswith("\\\\"):
        return EXTENDED_PREFIX + "UNC\\" + path[2:]
    return EXTENDED_PREFIX + path


def _ro(db):
    return sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True, timeout=30)


# ---------------------------------------------------------------------------
# Sauvegarde SQLite coherente
# ---------------------------------------------------------------------------

def sqlite_backup(src, dst):
    """Copie coherente via l'API de sauvegarde SQLite (instantane transactionnel).

    La source est ouverte en query_only : aucune ecriture possible depuis ce
    processus. L'API backup lit sous verrou partage ; une ecriture concurrente
    validee pendant la copie la fait recommencer, le resultat est donc toujours
    un etat valide de la base.
    """
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise FileNotFoundError(f"base source introuvable : {src}")
    if dst.exists():
        raise FileExistsError(f"destination deja presente (jamais ecrasee) : {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(src), timeout=60)
    try:
        source.execute("PRAGMA query_only = ON")
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target, pages=-1)
        finally:
            target.close()
    finally:
        source.close()
    check = integrity(dst)
    return {
        "method": "sqlite3-backup-api",
        "sqlite_version": sqlite3.sqlite_version,
        "dst": str(dst),
        "size": dst.stat().st_size,
        "sha256": sha256_file(dst),
        "integrity": check["integrity"],
        "ok": check["ok"],
    }


def integrity(db, tables=()):
    db = Path(db)
    if not db.exists():
        return {"ok": False, "integrity": "missing", "missing_tables": list(tables)}
    try:
        conn = _ro(db)
        try:
            rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "integrity": f"error: {exc}", "missing_tables": list(tables)}
    status = "ok" if rows == ["ok"] else "; ".join(rows[:5])
    missing = [t for t in tables if t not in names]
    return {"ok": status == "ok" and not missing, "integrity": status,
            "missing_tables": missing, "table_count": len(names)}


# ---------------------------------------------------------------------------
# Arborescences et manifest (chemins longs supportes)
# ---------------------------------------------------------------------------

def tree_copy(src, dst, exclude=(), overwrite=False):
    """Copie src -> dst. Sans overwrite, un fichier deja present n'est jamais remplace."""
    src_x, dst_x = _ext(src), _ext(dst)
    copied = skipped = identical = 0
    for current, dirs, files in os.walk(src_x):
        dirs[:] = sorted(d for d in dirs if d not in exclude)
        rel = os.path.relpath(current, src_x)
        target_dir = dst_x if rel == "." else os.path.join(dst_x, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in sorted(files):
            if name in exclude:
                continue
            source = os.path.join(current, name)
            target = os.path.join(target_dir, name)
            if os.path.exists(target) and not overwrite:
                same = (os.path.getsize(target) == os.path.getsize(source)
                        and sha256_file(target) == sha256_file(source))
                if same:
                    identical += 1
                else:
                    skipped += 1
                continue
            shutil.copy2(source, target)
            copied += 1
    return {"ok": True, "copied": copied, "skipped": skipped, "identical": identical}


def manifest_files(directory):
    base = _ext(directory)
    entries = []
    for current, dirs, files in os.walk(base):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(current, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            if rel == "manifest.json":
                continue
            entries.append({"path": rel, "size": os.path.getsize(full), "sha256": sha256_file(full)})
    entries.sort(key=lambda e: e["path"])
    return entries


def manifest_verify(directory, versions=(1,)):
    errors = []
    base = _ext(directory)
    manifest_path = os.path.join(base, "manifest.json")
    if not os.path.exists(manifest_path):
        return {"ok": False, "errors": ["manifest.json absent"]}
    try:
        with open(manifest_path, encoding="utf-8-sig") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return {"ok": False, "errors": ["manifest.json illisible"]}
    if manifest.get("format") != "lis-backup":
        errors.append(f"format inconnu : {manifest.get('format')}")
    if manifest.get("format_version") not in versions:
        errors.append(f"version de format non supportee : {manifest.get('format_version')}")
    listed = {}
    for entry in manifest.get("files") or []:
        listed[entry["path"]] = entry
        full = os.path.join(base, *entry["path"].split("/"))
        if not os.path.exists(full):
            errors.append(f"fichier manquant : {entry['path']}")
        elif os.path.getsize(full) != entry["size"]:
            errors.append(f"taille differente : {entry['path']}")
        elif sha256_file(full) != entry["sha256"]:
            errors.append(f"SHA-256 different : {entry['path']}")
    for entry in manifest_files(directory):
        if entry["path"] not in listed:
            errors.append(f"fichier non declare : {entry['path']}")
    return {"ok": not errors, "errors": errors, "files": len(listed)}


def remove_partial(directory):
    """Supprime UNIQUEMENT un dossier de backup inacheve (*.partial) cree par backup_lis."""
    path = os.path.abspath(str(directory))
    name = os.path.basename(path)
    if not (name.startswith("LIS-BACKUP-") and name.endswith(".partial")):
        raise ValueError("refus : seul un dossier LIS-BACKUP-*.partial peut etre supprime")
    shutil.rmtree(_ext(path))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sante du Vault (lecture seule)
# ---------------------------------------------------------------------------

def vault_health(root, db, semantic=False):
    root = Path(root).resolve()
    result = {}
    sys.path.insert(0, str(root))
    try:
        conn = _ro(db)
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "error": f"ouverture impossible : {exc}"}
    conn.row_factory = sqlite3.Row
    try:
        try:
            result["contents"] = conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
            result["sources"] = conn.execute(
                "SELECT COUNT(*) FROM intelligence_sources").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            result["count_error"] = str(exc)
        try:
            import vault_ask  # noqa: E402 - module du LIS, charge depuis root
        except Exception as exc:  # noqa: BLE001
            result["lexical"] = {"ok": False, "error": f"import vault_ask : {exc}"[:200]}
            return result
        started = time.perf_counter()
        try:
            answer = vault_ask.ask(conn, "test recherche outils", llm="off", semantic="off")
            result["lexical"] = {"ok": True, "sources": len(answer.get("sources") or []),
                                 "ms": round((time.perf_counter() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001
            result["lexical"] = {"ok": False, "error": str(exc)[:200]}
        if semantic:
            started = time.perf_counter()
            try:
                answer = vault_ask.ask(conn, "test recherche outils", llm="off",
                                       semantic="auto")
                kinds = [(s.get("retrieval") or {}).get("match_type")
                         for s in (answer.get("sources") or []) +
                         (answer.get("related_sources") or [])]
                result["semantic"] = {
                    "ok": True,
                    "used": any(k in ("semantic", "hybrid") for k in kinds),
                    "ms": round((time.perf_counter() - started) * 1000)}
            except Exception as exc:  # noqa: BLE001
                result["semantic"] = {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Configuration et secrets (jamais de valeur affichee)
# ---------------------------------------------------------------------------

def read_env_keys(path):
    values = {}
    path = Path(path)
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = bool(value.strip())
    return values


def env_status(path, keys):
    values = read_env_keys(path)
    if values is None:
        return {"env_file": False, "keys": {k: "NOT CONFIGURED" for k in keys}}
    return {"env_file": True,
            "keys": {k: ("CONFIGURED" if values.get(k) else "NOT CONFIGURED") for k in keys}}


_SECRET_NAMES = re.compile(
    r"(^|/)(\.env(\..+)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|google_token\.json|"
    r"google_credentials\.json|credentials\.json|token\.json|vault\.db|.*\.lisenc)$",
    re.IGNORECASE)
_SECRET_CONTENT = [
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("openrouter_key", re.compile(r"\bsk-or-v1-[0-9a-f]{32,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{32,}\b")),
    ("notion_token", re.compile(r"\b(secret_|ntn_)[A-Za-z0-9]{40,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
_ALLOWED_NAMES = {".env.example"}
# Valeurs factices des tests (jamais des cles reelles).
_FAKE_MARKERS = re.compile(r"FAKE|TEST|EXAMPLE|DUMMY|XXXX|0123456789ab", re.IGNORECASE)


def secret_scan(root, paths=None):
    """Fichiers suivis par Git qui ressemblent a des secrets. Ne renvoie que des noms."""
    root = Path(root).resolve()
    if paths is None:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                capture_output=True, check=True).stdout
        paths = [p for p in listed.decode("utf-8", "replace").split("\0") if p]
    findings = []
    for rel in paths:
        name = rel.replace("\\", "/")
        if Path(name).name in _ALLOWED_NAMES:
            continue
        if _SECRET_NAMES.search(name):
            findings.append({"file": name, "kind": "sensitive_filename"})
            continue
        target = root / name
        try:
            if not target.is_file() or target.stat().st_size > 2_000_000:
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kind, regex in _SECRET_CONTENT:
            if any(not _FAKE_MARKERS.search(m.group(0)) for m in regex.finditer(text)):
                findings.append({"file": name, "kind": kind})
                break
    return {"ok": not findings, "scanned": len(paths), "findings": findings}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Outils de deploiement du LIS")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sqlite-backup")
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True)
    p = sub.add_parser("integrity")
    p.add_argument("--db", required=True)
    p.add_argument("--tables", default="")
    p = sub.add_parser("vault-health")
    p.add_argument("--root", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--semantic", action="store_true")
    p = sub.add_parser("env-status")
    p.add_argument("--env", required=True)
    p.add_argument("--keys", required=True)
    p = sub.add_parser("secret-scan")
    p.add_argument("--root", required=True)
    p = sub.add_parser("tree-copy")
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True)
    p.add_argument("--exclude", default="")
    p.add_argument("--no-overwrite", action="store_true")
    p = sub.add_parser("manifest-files")
    p.add_argument("--dir", required=True)
    p = sub.add_parser("manifest-verify")
    p.add_argument("--dir", required=True)
    p.add_argument("--versions", default="1")
    p = sub.add_parser("remove-partial")
    p.add_argument("--dir", required=True)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "sqlite-backup":
            result = sqlite_backup(args.src, args.dst)
            return _out(result, 0 if result["ok"] else 2)
        if args.cmd == "integrity":
            tables = [t for t in args.tables.split(",") if t]
            result = integrity(args.db, tables)
            return _out(result, 0 if result["ok"] else 2)
        if args.cmd == "vault-health":
            return _out(vault_health(args.root, args.db, args.semantic))
        if args.cmd == "env-status":
            return _out(env_status(args.env, [k for k in args.keys.split(",") if k]))
        if args.cmd == "tree-copy":
            return _out(tree_copy(args.src, args.dst,
                                  tuple(x for x in args.exclude.split(",") if x),
                                  overwrite=not args.no_overwrite))
        if args.cmd == "manifest-files":
            return _out({"ok": True, "files": manifest_files(args.dir)})
        if args.cmd == "manifest-verify":
            result = manifest_verify(args.dir, tuple(int(v) for v in args.versions.split(",")))
            return _out(result, 0 if result["ok"] else 2)
        if args.cmd == "remove-partial":
            return _out(remove_partial(args.dir))
        if args.cmd == "secret-scan":
            result = secret_scan(args.root)
            return _out(result, 0 if result["ok"] else 2)
    except Exception as exc:  # noqa: BLE001 - erreur lisible pour PowerShell
        return _out({"ok": False, "error": str(exc)[:300]}, 3)
    return 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
