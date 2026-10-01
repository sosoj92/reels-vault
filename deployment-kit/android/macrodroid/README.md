# Export MacroDroid

**EXPORT REQUIRED FROM ANDROID DEVICE**

Aucun export réel n'est encore présent. Ne pas en fabriquer un à la main.

Pour le fournir :

1. MacroDroid → ouvrir la macro qui écrit les liens partagés dans `inbox.txt`.
2. Menu de la macro → **Exporter** → fichier `.macro`.
3. Copier ce fichier ici : `deployment-kit/android/macrodroid/Envoyer_au_Vault.macro`.
4. Vérifier qu'il ne contient ni mot de passe ni jeton (une macro de partage n'en a
   normalement aucun), puis committer.

`CHECK_LIS.ps1` passera alors la ligne `macrodroid export` à `OK`, et
`backup_lis.ps1` copiera l'export dans chaque backup (`macrodroid/`).
