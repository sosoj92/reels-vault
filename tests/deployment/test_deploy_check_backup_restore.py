"""CHECK_LIS.ps1, backup_lis.ps1, restore_lis.ps1, setup_lis.ps1 sur un faux LIS temporaire.

Aucun test n'ouvre le vrai vault.db : bases factices, dossiers temporaires, -NoSystem.
"""

from . import _helpers as h  # noqa: I001  (importe _isolation en premier)

import json
import shutil
import unittest

ENV_SECRETS = {"LIS_BACKUP_PASSPHRASE": h.FAKE_PASSPHRASE, "LIS_KDF_ITERATIONS": "10000"}


@unittest.skipUnless(h.POWERSHELL, "PowerShell requis")
class HealthCheckTest(unittest.TestCase):
    def check(self, db, extra_env=None):
        out = h.tmpdir(self) / "check.json"
        res = h.run_ps("CHECK_LIS.ps1", "-Root", h.REPO, "-Db", db, "-Python", h.PYTHON,
                       "-SkipSystem", "-SkipSemantic", "-NoReport", "-JsonOut", out,
                       "-BackupDir", h.tmpdir(self), env=extra_env, timeout=900)
        self.assertTrue(out.exists(), res.stdout + res.stderr)
        return res, h.read_json(out)

    def test_valid_fake_vault_is_read_only(self):
        db = h.fake_db(h.tmpdir(self) / "vault.db")
        before = h.sha256(db)
        secret = "sk-or-v1-" + "9f8e7d6c5b4a" * 4
        res, report = self.check(db, {"OPENROUTER_API_KEY": secret})
        r = h.rows(report)
        for name in ("sqlite integrity_check", "schema minimal", "retrieval lexical",
                     "vault.db lecture seule"):
            self.assertEqual(r[name]["Status"], "OK", (name, r[name]))
        self.assertEqual(r["provider OpenRouter"]["Status"], "OK")
        self.assertEqual(r["routage sensible"]["Status"], "OK")
        self.assertEqual(h.sha256(db), before, "le Health Check a modifie la base")
        self.assertNotIn(secret, res.stdout + res.stderr + json.dumps(report))
        self.assertIn("COMPONENT", res.stdout)
        self.assertIn(report["exit_code"], (0, 1, 2))
        self.assertEqual(res.returncode, report["exit_code"])

    def test_corrupted_vault_is_not_ready(self):
        db = h.tmpdir(self) / "vault.db"
        good = h.fake_db(h.tmpdir(self) / "good.db").read_bytes()
        db.write_bytes(good[:100] + b"\x00garbage" * 200 + good[2000:])
        res, report = self.check(db)
        self.assertEqual(h.rows(report)["sqlite integrity_check"]["Status"], "FAIL")
        self.assertEqual(res.returncode, 2)
        self.assertEqual(report["verdict"], "NOT READY")

    def test_missing_vault_is_not_ready(self):
        res, report = self.check(h.tmpdir(self) / "absent.db")
        self.assertEqual(h.rows(report)["vault.db"]["Status"], "FAIL")
        self.assertEqual(res.returncode, 2)


@unittest.skipUnless(h.POWERSHELL, "PowerShell requis")
class BackupRestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = h.tmpdir(self)
        self.lis = h.fake_lis(self.tmp / "lis")
        self.dest = self.tmp / "backups"

    def backup(self, *extra, env=None):
        res = h.run_ps("backup_lis.ps1", "-Root", self.lis, "-Destination", self.dest,
                       "-Python", h.PYTHON, "-NoSystem", *extra, env=env)
        return res

    def only_backup(self):
        found = [p for p in self.dest.iterdir() if p.name.startswith("LIS-BACKUP-")]
        self.assertEqual(len(found), 1, found)
        self.assertFalse(found[0].name.endswith(".partial"))
        return found[0]

    def restore(self, backup, root, *extra, env=None):
        return h.run_ps("restore_lis.ps1", "-BackupPath", backup, "-Root", root,
                        "-Python", h.PYTHON, "-NoSystem", "-SkipInstall", "-SkipHealthCheck",
                        *extra, env=env)

    # ------------------------------------------------------------------ backup
    def test_backup_is_consistent_and_secret_free(self):
        source_hash = h.sha256(self.lis / "vault.db")
        res = self.backup()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        bk = self.only_backup()
        manifest = h.read_json(bk / "manifest.json")
        self.assertEqual(manifest["format"], "lis-backup")
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["sqlite_backup"]["method"], "sqlite3-backup-api")
        self.assertEqual(manifest["sqlite_backup"]["integrity"], "ok")
        self.assertFalse(manifest["secrets"]["included"])
        listed = {f["path"]: f for f in manifest["files"]}
        for rel in ("vault.db", "lis.lock.json", "installation-report.json",
                    "personal-files/raw/fiche-test.md", "personal-files/index.md",
                    "personal-files/mobile-inbox/inbox.txt", "personal-files/jarvis/notes/note.txt",
                    "config/env.redacted"):
            self.assertIn(rel, listed)
            self.assertEqual(h.sha256(bk / rel), listed[rel]["sha256"], rel)
        self.assertEqual(h.count_contents(bk / "vault.db"), h.count_contents(self.lis / "vault.db"))
        self.assertEqual(h.sha256(self.lis / "vault.db"), source_hash)
        everything = b"".join(p.read_bytes() for p in bk.rglob("*") if p.is_file())
        self.assertNotIn(h.FAKE_TOKEN.encode(), everything)
        self.assertNotIn(b"jarvis-secret-value-xyz", everything)
        self.assertFalse(any(p.name == ".env" for p in bk.rglob("*")))
        redacted = (bk / "config" / "env.redacted").read_text(encoding="utf-8-sig")
        self.assertIn("TELEGRAM_BOT_TOKEN=\n", redacted.replace("\r\n", "\n"))
        self.assertIn("OLLAMA_MODEL=qwen3:1.7b", redacted)

    def test_long_paths_over_260_chars(self):
        # Regression : images/ contient des noms de videos > 260 caracteres
        # (LongPathsEnabled = 0 sur le PC de reference).
        import os
        folder = self.lis / "images" / ("tiktok_" + "7" * 19)
        name = "7" * 19 + " " + "titre tres long de video " * 8 + ".mp4"
        target = "\\\\?\\" + str(folder / name)
        os.makedirs("\\\\?\\" + str(folder), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"video factice")
        self.assertGreater(len(str(folder / name)), 260)
        res = self.backup()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        bk = self.only_backup()
        manifest = h.read_json(bk / "manifest.json")
        self.assertTrue(any(f["path"].endswith(name) for f in manifest["files"]))
        restored = self.tmp / "restored-long"
        out = self.restore(bk, restored)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        copy = "\\\\?\\" + str(restored / "images" / folder.name / name)
        with open(copy, "rb") as handle:
            self.assertEqual(handle.read(), b"video factice")

    def test_backup_dry_run_writes_nothing(self):
        res = self.backup("-DryRun", "-IncludeSecrets")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertFalse(self.dest.exists())

    def test_backup_with_secrets_is_encrypted(self):
        res = self.backup("-IncludeSecrets", env=ENV_SECRETS)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        bk = self.only_backup()
        box = (bk / "secrets" / "secrets.lisenc").read_bytes()
        self.assertNotIn(h.FAKE_TOKEN.encode(), box)
        self.assertNotIn(b"jarvis-secret-value-xyz", box)
        self.assertNotIn(h.FAKE_PASSPHRASE.encode(), box)
        self.assertEqual(json.loads(box)["cipher"], "AES-256-CBC")
        self.assertFalse(any(p.name in (".env", "config.yaml") for p in bk.rglob("*")))
        self.assertNotIn(h.FAKE_PASSPHRASE, res.stdout + res.stderr)
        self.assertNotIn(h.FAKE_TOKEN, res.stdout + res.stderr)
        self.assertTrue(h.read_json(bk / "manifest.json")["secrets"]["encrypted"])

    # ----------------------------------------------------------------- restore
    def test_restore_into_empty_temp_root(self):
        self.assertEqual(self.backup().returncode, 0)
        bk = self.only_backup()
        verify = h.run_ps("restore_lis.ps1", "-BackupPath", bk, "-Root", self.tmp / "verify",
                          "-Python", h.PYTHON, "-VerifyOnly", "-NoSystem")
        self.assertEqual(verify.returncode, 0, verify.stdout)
        self.assertIn("BACKUP VERIFIED", verify.stdout)
        self.assertFalse((self.tmp / "verify" / "vault.db").exists())

        target = self.tmp / "restored"
        res = self.restore(bk, target)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("DATA RESTORED", res.stdout)
        self.assertNotIn("LIS RESTORED", res.stdout)
        self.assertEqual(h.count_contents(target / "vault.db"), h.count_contents(self.lis / "vault.db"))
        self.assertEqual((target / "raw" / "fiche-test.md").read_text(encoding="utf-8"), "# Fiche factice\n")
        self.assertTrue((target / "mobile-inbox" / "inbox.txt").exists())
        env = (target / ".env").read_text(encoding="utf-8-sig")
        self.assertNotIn(h.FAKE_TOKEN, env)
        self.assertIn("OLLAMA_MODEL=qwen3:1.7b", env)
        # Idempotence : relancer reconnait la base identique au backup.
        again = self.restore(bk, target)
        self.assertEqual(again.returncode, 1, again.stdout)
        self.assertIn("deja restauree", again.stdout)
        self.assertNotIn("different(s) deja presents", again.stdout)
        # Une base qui a vecu depuis n'est jamais ecrasee sans -ReplaceVault.
        h.fake_db(self.tmp / "other.db", sources=1)
        changed = (self.tmp / "other.db").read_bytes()
        (target / "vault.db").write_bytes(changed)
        blocked = self.restore(bk, target)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("BLOCK", blocked.stdout)
        self.assertEqual((target / "vault.db").read_bytes(), changed)

    def test_restore_never_overwrites_existing_vault_or_env(self):
        self.assertEqual(self.backup("-IncludeSecrets", env=ENV_SECRETS).returncode, 0)
        bk = self.only_backup()
        target = self.tmp / "existing"
        target.mkdir()
        h.fake_db(target / "vault.db", sources=1)
        (target / ".env").write_text("MON_ENV=perso\n", encoding="utf-8")
        db_hash = h.sha256(target / "vault.db")

        blocked = self.restore(bk, target, "-RestoreSecrets", env=ENV_SECRETS)
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(h.sha256(target / "vault.db"), db_hash)

        (target / "external" / "jarvis-assistant-vocal").mkdir(parents=True)
        res = self.restore(bk, target, "-RestoreSecrets", "-ReplaceVault", env=ENV_SECRETS)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual((target / ".env").read_text(encoding="utf-8"), "MON_ENV=perso\n")
        moved = list((target / "backups").glob("vault-before-restore-*/vault.db"))
        self.assertEqual(len(moved), 1)
        self.assertEqual(h.sha256(moved[0]), db_hash)
        self.assertEqual(h.count_contents(target / "vault.db"), 3)
        cfg = target / "external" / "jarvis-assistant-vocal" / "config.yaml"
        self.assertEqual(cfg.read_text(encoding="utf-8"), "api_key: jarvis-secret-value-xyz\n")
        self.assertNotIn(h.FAKE_PASSPHRASE, res.stdout + res.stderr)
        self.assertNotIn("jarvis-secret-value-xyz", res.stdout + res.stderr)

    def test_restore_secrets_wrong_passphrase_writes_nothing(self):
        self.assertEqual(self.backup("-IncludeSecrets", env=ENV_SECRETS).returncode, 0)
        bk = self.only_backup()
        target = self.tmp / "wrongpass"
        (target / "external" / "jarvis-assistant-vocal").mkdir(parents=True)
        res = self.restore(bk, target, "-RestoreSecrets",
                           env={"LIS_BACKUP_PASSPHRASE": "mauvaise passphrase !!"})
        self.assertIn("HMAC invalide", res.stdout)
        self.assertFalse((target / "external" / "jarvis-assistant-vocal" / "config.yaml").exists())
        self.assertNotIn(h.FAKE_TOKEN, (target / ".env").read_text(encoding="utf-8-sig"))

    def test_tampered_backup_is_refused(self):
        self.assertEqual(self.backup().returncode, 0)
        bk = self.only_backup()
        cases = {
            "modified": lambda d: (d / "personal-files" / "index.md").write_text("pirate\n", encoding="utf-8"),
            "extra": lambda d: (d / "personal-files" / "intrus.ps1").write_text("x", encoding="utf-8"),
            "removed": lambda d: (d / "lis.lock.json").unlink(),
        }
        for name, tamper in cases.items():
            with self.subTest(name):
                copy = self.tmp / f"tampered-{name}"
                shutil.copytree(bk, copy)
                tamper(copy)
                target = self.tmp / f"target-{name}"
                res = self.restore(copy, target)
                self.assertEqual(res.returncode, 2, res.stdout)
                self.assertIn("BACKUP REFUSE", res.stdout)
                self.assertFalse((target / "vault.db").exists())

    def test_unsupported_format_version_is_refused(self):
        self.assertEqual(self.backup().returncode, 0)
        bk = self.only_backup()
        manifest = h.read_json(bk / "manifest.json")
        manifest["format_version"] = 99
        (bk / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        res = self.restore(bk, self.tmp / "v99")
        self.assertEqual(res.returncode, 2)
        self.assertIn("non supportee", res.stdout)


@unittest.skipUnless(h.POWERSHELL, "PowerShell requis")
class SetupLisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = h.tmpdir(self)
        self.lis = h.fake_lis(self.tmp / "lis", with_secrets=False)
        self.reports = self.tmp / "reports"

    def snapshot(self):
        return {str(p.relative_to(self.lis)): p.read_bytes() for p in self.lis.rglob("*") if p.is_file()}

    def setup(self, *args):
        return h.run_ps("setup_lis.ps1", "-Root", self.lis, "-ReportDir", self.reports, *args, timeout=900)

    def test_requires_a_mode(self):
        self.assertEqual(self.setup().returncode, 2)

    def test_dry_run_and_audit_change_nothing(self):
        before = self.snapshot()
        for mode in ("-DryRun", "-Audit"):
            res = self.setup(mode)
            self.assertIn(res.returncode, (1, 2), res.stdout)
            self.assertIn("INSTALL", res.stdout)          # .venv, upstreams... a installer
            self.assertIn(".env", res.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.lis / ".env").exists())
        self.assertFalse((self.lis / ".venv").exists())

    def test_env_created_from_example_and_never_overwritten(self):
        res = self.setup("-Install", "-Only", "env")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual((self.lis / ".env").read_bytes(), (self.lis / ".env.example").read_bytes())
        (self.lis / ".env").write_text("CLE=perso\n", encoding="utf-8")
        again = self.setup("-Install", "-Only", "env")
        self.assertEqual(again.returncode, 0)
        self.assertIn("SKIP", again.stdout)
        self.assertEqual((self.lis / ".env").read_text(encoding="utf-8"), "CLE=perso\n")

    def test_mobile_inbox_install_is_idempotent(self):
        shutil.rmtree(self.lis / "mobile-inbox")
        first = self.setup("-Install", "-Only", "mobile-inbox")
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertTrue((self.lis / "mobile-inbox" / "inbox.txt").exists())
        (self.lis / "mobile-inbox" / "inbox.txt").write_text("lien\n", encoding="utf-8")
        second = self.setup("-Install", "-Only", "mobile-inbox")
        self.assertIn("SKIP", second.stdout)
        self.assertEqual((self.lis / "mobile-inbox" / "inbox.txt").read_text(encoding="utf-8"), "lien\n")

    def test_existing_vault_is_never_replaced(self):
        before = h.sha256(self.lis / "vault.db")
        res = self.setup("-Install", "-Only", "vault")
        self.assertIn("jamais remplace", res.stdout)
        self.assertEqual(h.sha256(self.lis / "vault.db"), before)


if __name__ == "__main__":
    unittest.main()
