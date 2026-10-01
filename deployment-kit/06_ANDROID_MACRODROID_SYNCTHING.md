# 06 — Android : MacroDroid + Syncthing

Pipeline final :

~~~text
TikTok / Instagram / autre app
 → Partager
 → MacroDroid
 → inbox.txt
 → Syncthing
 → PC : mobile-inbox/inbox.txt
 → inbox.bat
 → ingestion
 → vault.db
~~~

Tailscale n'est pas nécessaire.

Détails, configuration exacte et emplacement de l'export : `android/README.md`,
`android/syncthing/README.md`, `android/macrodroid/README.md`
(**EXPORT REQUIRED FROM ANDROID DEVICE** tant qu'aucun `.macro` réel n'y est déposé).

Côté Windows, `setup_lis.ps1 -Install` crée `mobile-inbox\inbox.txt` et installe Syncthing
(`winget install Syncthing.Syncthing`) ; `CHECK_LIS.ps1` vérifie qu'un dossier Syncthing
pointe vers `mobile-inbox`.

## Syncthing
Associer téléphone et PC, puis synchroniser un dossier dédié au LIS vers `mobile-inbox`
(label de référence « Reels Vault Inbox », type **Recevoir seulement** côté PC).

Ne pas synchroniser `.env` ni `vault.db` en écriture simultanée entre appareils.

## MacroDroid
La macro reçoit l'URL depuis la feuille de partage et l'ajoute sur une nouvelle ligne dans `inbox.txt`. Documenter les permissions Android accordées.

## Test bout-en-bout
1. Partager un vrai TikTok/Instagram.
2. Vérifier écriture Android.
3. Vérifier Syncthing.
4. Vérifier le fichier sur PC.
5. Lancer/attendre ingestion.
6. Vérifier la source dans `vault.db`.
7. Vérifier la projection Notion au passage suivant.

La synchro seule ne suffit pas : le contenu doit arriver dans le Vault.
