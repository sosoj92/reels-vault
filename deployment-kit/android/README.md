# Android : MacroDroid + Syncthing

Pipeline de référence (le seul maintenu ; l'ancien raccourci iPhone/iCloud est
**legacy**, voir `docs/legacy/`) :

```
TikTok / Instagram
  → Partager
  → MacroDroid (macro « Envoyer au Vault »)
  → inbox.txt (dossier synchronisé sur le téléphone)
  → Syncthing
  → PC : <repo>\mobile-inbox\inbox.txt
  → inbox.bat (tâche « Reels Vault - Hourly », toutes les heures)
  → ingest.py / vault_refresh.py / vault_ingestion.py
  → vault.db
```

## Ce qui est versionné ici

| Dossier | Contenu | État |
|---|---|---|
| `macrodroid/` | export réel de la macro (`*.macro`) | **EXPORT REQUIRED FROM ANDROID DEVICE** |
| `syncthing/` | configuration attendue du dossier partagé | documentée |

Aucun export MacroDroid n'a été inventé : tant qu'aucun fichier `.macro` réel n'est
déposé dans `macrodroid/`, `CHECK_LIS.ps1` affiche `MANUAL TEST REQUIRED`.

## Côté Windows (automatisé)

| Élément | Valeur | Créé par |
|---|---|---|
| Dossier | `<repo>\mobile-inbox` | `setup_lis.ps1 -Install` (étape `mobile-inbox`) |
| Fichier lu | `<repo>\mobile-inbox\inbox.txt` | idem (vide) ou Syncthing |
| Syncthing | `winget install Syncthing.Syncthing` | `setup_lis.ps1 -Install` (étape `syncthing`) |
| Ingestion | `inbox.bat` toutes les heures | `setup_windows_tasks.ps1` |

`inbox.bat` se place toujours dans le dossier du repo et utilise `.venv` : il
fonctionne même si le repo est déplacé.

## Côté téléphone (manuel)

1. Installer **Syncthing** (application Android) et **MacroDroid**.
2. Appairer le téléphone avec le PC (Syncthing → Ajouter un appareil → ID du PC,
   affiché dans l'interface Syncthing du PC : http://127.0.0.1:8384).
3. Partager le dossier du téléphone qui contient `inbox.txt` avec le PC — voir
   `syncthing/README.md` pour le type de dossier et l'ID.
4. Importer la macro MacroDroid (`macrodroid/*.macro`) ou la recréer : elle doit
   recevoir le lien partagé depuis TikTok/Instagram et l'**ajouter en fin de ligne**
   dans `inbox.txt` du dossier synchronisé (un lien par ligne, sans écraser le fichier).
5. Exporter la macro (MacroDroid → la macro → Exporter) et déposer le `.macro`
   dans `deployment-kit/android/macrodroid/` pour la prochaine réinstallation.

## Test de bout en bout (MANUAL TEST REQUIRED)

1. Sur le téléphone : TikTok → Partager → macro « Envoyer au Vault ».
2. Sur le PC, sous ~1 minute :
   `Get-Content .\mobile-inbox\inbox.txt -Tail 3` → le lien apparaît.
3. Lancer l'ingestion sans attendre l'heure : `.\inbox.bat`
4. Vérifier : `.\.venv\Scripts\python.exe vault_ask.py "<mot du titre>" --no-llm`
   ou `.\CHECK_LIS.ps1` (le nombre de contenus augmente).

## Restauration (PC neuf)

`backup_lis.ps1` sauvegarde `syncthing/syncthing-mapping.json` (ID et type des
dossiers, appareils — **sans clé**) et, avec `-IncludeSecrets`, l'identité Syncthing
du PC (`cert.pem` / `key.pem`, chiffrés). `restore_lis.ps1 -RestoreSecrets` remet cette
identité **avant** le premier lancement de Syncthing : le téléphone reconnaît alors le
PC sans nouvel appairage. Sinon, `restore_lis.ps1` indique l'ID de dossier à recréer.
