# Syncthing : dossier « Reels Vault Inbox »

Configuration validée sur le PC de référence (Syncthing 2.1.5, installé par winget,
démarré par le raccourci `Syncthing.lnk` du dossier Démarrage) :

| Paramètre | PC (Windows) | Téléphone (Android) |
|---|---|---|
| Label | `Reels Vault Inbox` | idem |
| Chemin | `<repo>\mobile-inbox` | dossier où MacroDroid écrit `inbox.txt` |
| Type | **Recevoir seulement** (`receiveonly`) | Envoyer seulement (ou Envoyer & recevoir) |
| Surveillance | activée (fsWatcher, 10 s) | activée |
| ID du dossier | identique des deux côtés (voir `syncthing-mapping.json` du backup) | |

Le PC est en *réception seule* : une modification locale de `mobile-inbox` ne peut
jamais repartir vers le téléphone.

L'ID du dossier et les ID d'appareils ne sont pas versionnés ici (identifiants de tes
appareils) : ils sont dans chaque backup (`syncthing/syncthing-mapping.json`).

## Vérifications

- `.\CHECK_LIS.ps1` → ligne `syncthing` : `OK` si un dossier Syncthing pointe vers
  `mobile-inbox`.
- Interface : http://127.0.0.1:8384 → dossier « Reels Vault Inbox » « À jour ».
- Le marqueur `mobile-inbox\.stfolder` doit exister (créé par Syncthing).
