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

## Syncthing
Associer téléphone et PC, puis synchroniser un dossier dédié au LIS vers `mobile-inbox`.

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
