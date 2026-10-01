"""Lock file, setup_upstreams.ps1 (SHA exacts, depot propre/dirty), pont Jarvis, syntaxe."""

from . import _helpers as h  # noqa: I001  (importe _isolation en premier)

import json
import re
import subprocess
import unittest
from pathlib import Path

import install_jarvis_voice_bridge as bridge

SHA = re.compile(r"^[0-9a-f]{40}$")


class LockFileTest(unittest.TestCase):
    def setUp(self):
        self.lock = h.read_json(h.REPO / "deployment-kit" / "lis.lock.json")

    def test_schema_and_shas(self):
        self.assertEqual(self.lock["schema"], "lis-lock")
        self.assertEqual(self.lock["schema_version"], 1)
        self.assertRegex(self.lock["lis"]["validated_sha"], SHA)
        names = {u["name"] for u in self.lock["upstreams"]}
        self.assertEqual(names, {"agency-agents", "reels-vault-upstream", "jarvis-assistant-vocal"})
        for up in self.lock["upstreams"]:
            self.assertRegex(up["sha"], SHA, up["name"])
            self.assertTrue(up["url"].startswith("https://github.com/"))

    def test_reference_stack(self):
        self.assertEqual(self.lock["ollama"]["required_model"], "qwen3:1.7b")
        self.assertEqual(self.lock["embeddings"]["model"], "multilingual-e5-small")
        text = json.dumps(self.lock).lower()
        self.assertNotIn("tailscale", text)
        self.assertNotIn("icloud", text)

    def test_installed_upstreams_match_lock_when_present(self):
        for up in self.lock["upstreams"]:
            path = h.REPO / up["path"]
            if not (path / ".git").exists():
                continue  # CI : upstreams non clones
            head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            self.assertEqual(head, up["sha"], up["name"])

    @unittest.skipUnless(h.POWERSHELL, "PowerShell requis")
    def test_powershell_lock_parser_rejects_bad_sha(self):
        tmp = h.tmpdir(self)
        bad = dict(self.lock)
        bad["upstreams"] = [dict(self.lock["upstreams"][0], sha="main")]
        (tmp / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
        cmd = (f"Import-Module '{h.REPO / 'deployment-kit/lib/LisDeploy.psm1'}'; "
               f"try {{ Read-LisLock -Path '{tmp / 'bad.json'}' | Out-Null; 'ACCEPTED' }} "
               f"catch {{ 'REJECTED' }}; Read-LisLock -Path '{h.REPO / 'deployment-kit/lis.lock.json'}' | "
               "ForEach-Object { 'OK ' + $_.upstreams.Count }")
        out = subprocess.run([h.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                             capture_output=True, text=True).stdout
        self.assertIn("REJECTED", out)
        self.assertIn("OK 3", out)


@unittest.skipUnless(h.POWERSHELL, "PowerShell requis")
class PowerShellSyntaxTest(unittest.TestCase):
    SCRIPTS = ["CHECK_LIS.ps1", "backup_lis.ps1", "restore_lis.ps1", "setup_lis.ps1",
               "setup_upstreams.ps1", "setup_windows_tasks.ps1", "setup_semantic_model.ps1",
               "deployment-kit/lib/LisDeploy.psm1"]

    def test_parse_without_errors(self):
        for script in self.SCRIPTS:
            path = h.REPO / script
            cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{path}', [ref]$null, [ref]$e); $e.Count")
            out = subprocess.run([h.POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                                 capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, "0", script)

    def test_scripts_are_ascii(self):
        # Windows PowerShell 5.1 lit un .ps1 sans BOM en ANSI : ASCII obligatoire.
        for script in self.SCRIPTS:
            data = (h.REPO / script).read_bytes()
            self.assertTrue(all(b < 128 for b in data), script)


@unittest.skipUnless(h.POWERSHELL, "PowerShell requis")
class SetupUpstreamsTest(unittest.TestCase):
    """Upstream factice local : jamais de reseau, jamais les vrais external/."""

    def setUp(self):
        self.tmp = h.tmpdir(self)
        self.src = self.tmp / "upstream-src"
        self.src.mkdir()
        h.git("init", "-q", cwd=self.src)
        (self.src / "a.txt").write_text("v1\n", encoding="utf-8")
        (self.src / "managed.txt").write_text("m1\n", encoding="utf-8")
        h.git("add", "-A", cwd=self.src)
        h.git("commit", "-q", "-m", "v1", cwd=self.src)
        self.sha1 = h.git("rev-parse", "HEAD", cwd=self.src)
        (self.src / "a.txt").write_text("v2\n", encoding="utf-8")
        h.git("commit", "-q", "-am", "v2", cwd=self.src)
        self.sha2 = h.git("rev-parse", "HEAD", cwd=self.src)
        self.root = self.tmp / "lis"
        self.root.mkdir()
        self.lockfile = self.tmp / "lock.json"
        self.reports = self.tmp / "reports"
        self.write_lock(self.sha1)

    def write_lock(self, sha):
        lock = h.read_json(h.REPO / "deployment-kit" / "lis.lock.json")
        lock["upstreams"] = [{"name": "demo", "url": str(self.src), "path": "external/demo",
                              "sha": sha, "managed_paths": ["managed.txt"]}]
        self.lockfile.write_text(json.dumps(lock), encoding="utf-8")

    def run_setup(self, *extra):
        return h.run_ps("setup_upstreams.ps1", "-Root", self.root, "-LockFile", self.lockfile,
                        "-ReportDir", self.reports, *extra)

    def head(self):
        return h.git("rev-parse", "HEAD", cwd=self.root / "external" / "demo")

    def test_clone_exact_sha_then_idempotent(self):
        first = self.run_setup()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.head(), self.sha1)  # pas origin/main (= sha2)
        second = self.run_setup()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("conforme", second.stdout)
        self.assertEqual(self.head(), self.sha1)

    def test_audit_and_dryrun_change_nothing(self):
        for mode in ("-Audit", "-DryRun"):
            res = self.run_setup(mode)
            self.assertEqual(res.returncode, 1, res.stdout)
            self.assertFalse((self.root / "external" / "demo").exists())
        self.run_setup()
        self.write_lock(self.sha2)
        res = self.run_setup("-Audit")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.head(), self.sha1)

    def test_lock_update_checks_out_new_sha_and_keeps_untracked(self):
        self.run_setup()
        demo = self.root / "external" / "demo"
        (demo / "perso.local").write_text("garde-moi\n", encoding="utf-8")
        (demo / "managed.txt").write_text("modifie par le bridge\n", encoding="utf-8")
        self.write_lock(self.sha2)
        res = self.run_setup()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.head(), self.sha2)
        self.assertEqual((demo / "perso.local").read_text(encoding="utf-8"), "garde-moi\n")
        patches = list((self.root / "backups" / "upstream-patches").glob("demo-*.patch"))
        self.assertEqual(len(patches), 1)

    def test_dirty_repo_blocks_without_force(self):
        self.run_setup()
        demo = self.root / "external" / "demo"
        (demo / "a.txt").write_text("modification locale\n", encoding="utf-8")
        self.write_lock(self.sha2)
        res = self.run_setup()
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("BLOCK", res.stdout)
        self.assertEqual(self.head(), self.sha1)
        self.assertEqual((demo / "a.txt").read_text(encoding="utf-8"), "modification locale\n")
        forced = self.run_setup("-Force")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual(self.head(), self.sha2)
        patch = next((self.root / "backups" / "upstream-patches").glob("demo-*.patch"))
        self.assertIn("modification locale", patch.read_text(encoding="utf-8"))

    def test_existing_non_git_folder_is_never_deleted(self):
        target = self.root / "external" / "demo"
        target.mkdir(parents=True)
        (target / "precieux.txt").write_text("x", encoding="utf-8")
        res = self.run_setup("-ForceRefresh")
        self.assertEqual(res.returncode, 2)
        self.assertTrue((target / "precieux.txt").exists())

    def test_wrong_remote_blocks(self):
        self.run_setup()
        other = self.tmp / "other"
        other.mkdir()
        h.git("init", "-q", cwd=other)
        h.git("remote", "set-url", "origin", str(other), cwd=self.root / "external" / "demo")
        res = self.run_setup()
        self.assertEqual(res.returncode, 2)
        self.assertIn("remote origin inattendu", res.stdout)


class JarvisBridgeTest(unittest.TestCase):
    ROUTER = ('def decider_prioritaire(phrase):\n'
              '    # Caméra/gestes : commandes locales explicites, avant toute interprétation\n'
              '    return None\n\n_DOMAINES = (\n' + bridge.ROUTE_ANCHOR + ')\n')
    BRIEF = ("def faire_brief():\n    morceaux = []\n" + bridge.BRIEF_ANCHOR +
             "        morceaux.append('mails')\n    return ' '.join(morceaux)\n")

    def make_jarvis(self, brief=None):
        jarvis = h.tmpdir(self) / "jarvis"
        (jarvis / "core").mkdir(parents=True)
        (jarvis / "tools").mkdir()
        (jarvis / "core" / "routage_intentions.py").write_text(self.ROUTER, encoding="utf-8")
        (jarvis / "tools" / "brief.py").write_text(brief or self.BRIEF, encoding="utf-8")
        return jarvis

    def test_install_is_complete_and_idempotent(self):
        jarvis = self.make_jarvis()
        self.assertFalse(bridge.check(jarvis)["installed"])
        first = bridge.install(jarvis)
        self.assertIn("tools/brief.py", first["changed"])
        self.assertIn("tools/life_intelligence.py", first["changed"])
        state = bridge.check(jarvis)
        self.assertTrue(state["installed"], state)
        self.assertEqual(set(state["modules"]),
                         {p.name for p in bridge.integration_modules()})
        self.assertEqual(bridge.install(jarvis)["changed"], [])
        router = (jarvis / "core" / "routage_intentions.py").read_text(encoding="utf-8")
        self.assertEqual(router.count(bridge.MARKER), 1)
        self.assertEqual(router.count(bridge.PRIORITY_MARKER), 1)

    def test_legacy_brief_hook_is_not_duplicated(self):
        legacy = self.BRIEF.replace(bridge.BRIEF_ANCHOR, "    try:\n        " +
                                    bridge.BRIEF_LEGACY_SIGNATURE + "\n    except Exception:\n"
                                    "        pass\n" + bridge.BRIEF_ANCHOR)
        jarvis = self.make_jarvis(brief=legacy)
        bridge.install(jarvis)
        text = (jarvis / "tools" / "brief.py").read_text(encoding="utf-8")
        self.assertEqual(text.count(bridge.BRIEF_LEGACY_SIGNATURE), 1)
        self.assertNotIn(bridge.BRIEF_MARKER, text)

    def test_check_never_writes(self):
        jarvis = self.make_jarvis()
        before = {p: p.read_bytes() for p in jarvis.rglob("*") if p.is_file()}
        bridge.check(jarvis)
        after = {p: p.read_bytes() for p in jarvis.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_missing_anchor_fails_before_any_write(self):
        jarvis = self.make_jarvis(brief="def faire_brief():\n    return ''\n")
        with self.assertRaises(bridge.InstallError):
            bridge.install(jarvis)
        self.assertFalse((jarvis / "tools" / "life_intelligence.py").exists())


class SecretScanTest(unittest.TestCase):
    def test_detects_tracked_secrets_and_ignores_fake_fixtures(self):
        sys_path = str(h.REPO / "deployment-kit" / "lib")
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        import lis_deploy
        repo = h.tmpdir(self)
        h.git("init", "-q", cwd=repo)
        (repo / "ok.py").write_text('KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"\n', encoding="utf-8")
        leaked = "987654321:AAH" + "x9Kq" * 8  # forme d'un vrai jeton Telegram
        (repo / "leak.py").write_text(f'TOKEN = "{leaked}"\n', encoding="utf-8")
        (repo / ".env").write_text("X=1\n", encoding="utf-8")
        (repo / ".env.example").write_text("X=\n", encoding="utf-8")
        h.git("add", "-f", "-A", cwd=repo)
        result = lis_deploy.secret_scan(repo)
        files = {f["file"] for f in result["findings"]}
        self.assertEqual(files, {"leak.py", ".env"})
        self.assertNotIn("x9Kq", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
