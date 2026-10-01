# 03 — Telegram

Telegram = capture, questions, vocaux, fichiers et alertes.

## Configuration
L'humain crée le bot avec BotFather. Ne jamais coller le token dans GitHub ou un chat.

~~~powershell
.\.venv\Scripts\python.exe telegram_setup.py
~~~

Le script saisit le token masqué, vérifie le bot, attend /start, détecte user_id/chat_id, applique la whitelist, écrit `.env` et envoie un message de test.

## Test manuel

~~~powershell
.\.venv\Scripts\python.exe telegram_gateway.py
~~~

Tester :
- question simple ;
- lien TikTok/Instagram ;
- note « garde ça » ;
- fichier ;
- demande longue ;
- réponse longue ;
- Agency Agent nommé.

## Garanties
- whitelist user + chat ;
- aucune commande shell exposée ;
- messages longs découpés sans perte ;
- contexte conservé ;
- sources non fiables ;
- sensible jamais envoyé silencieusement au cloud.

## Runtime
La gateway est ensuite gérée par `setup_windows_tasks.ps1`. `setup_lis.ps1 -Install` ne crée
cette tâche que lorsque Telegram est configuré ; après `telegram_setup.py` :

~~~powershell
.\setup_windows_tasks.ps1 -Only TelegramGateway
~~~

Après tout changement de code, redémarrer réellement la gateway puis faire un smoke test.
`.\CHECK_LIS.ps1` vérifie la configuration (sans afficher de valeur), la whitelist user + chat,
la tâche (ouverture de session, redémarrage, `.venv`) et l'absence de commande shell.
