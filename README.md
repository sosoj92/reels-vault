# Transformer ses vidéos Instagram sauvegardées en base de données interrogeable

*Tuto complet, testé sur Windows 11. Zéro connaissance en code requise. Coût : 0 € si tu as déjà un abonnement Claude.*

---

## Le problème

Tu as des centaines de Reels sauvegardés. Des restos, des adresses, des astuces, des idées de voyage. Et tu ne retrouves jamais rien, parce que tout est enfermé dans des vidéos qu'aucune recherche ne peut lire.

À la fin de ce tuto, tu pourras demander en langage normal : *« je pars au Japon, sors-moi tout ce que j'ai gardé »* — et obtenir la liste, avec les adresses, les horaires et les prix.

Le principe : chaque vidéo est **téléchargée, transcrite et résumée**, puis rangée dans une base de fichiers texte. Et une fois le système en place, tu partages un Reel depuis ton téléphone et il s'ajoute tout seul pendant la nuit.

---

## Ce que ça coûte vraiment

Le montage d'origine (celui qui a tourné sur X) repose sur un agent nommé Hermes, branché à une clé API facturée au token, sur une machine allumée en permanence.

La version de ce tuto ne coûte **rien de plus** qu'un abonnement Claude Pro, parce qu'elle remplace chaque brique payante par un équivalent gratuit :

| Le montage d'origine | Ce qu'on utilise à la place | Coût |
|---|---|---|
| Hermes + clé API | **Claude Cowork** (inclus dans Pro) | 0 € |
| Agent iMessage 24h/24 | **Un raccourci iPhone + une tâche planifiée** | 0 € |
| Serveur allumé en permanence | **Ton PC, quand il est allumé** | 0 € |
| Transcription par API | **Whisper en local** | 0 € |
| Téléchargement des vidéos | **yt-dlp** | 0 € |

**La seule vraie limite** : les quotas d'usage de Claude. Compter quelques centaines de vidéos par session confortablement. Traiter 10 000 vidéos d'un coup n'est pas réaliste sur un abonnement Pro — mais 300 fiches bien rangées servent davantage que 10 000 en vrac.

---

## Ce qu'il te faut

- Un PC Windows
- Un abonnement **Claude Pro**
- **Firefox** (et non Chrome — l'explication est plus bas, c'est important)
- Un iPhone, pour la partie « envoyer depuis son téléphone » (facultatif)

---

## Étape 1 — Installer les outils

Ouvre **PowerShell** (touche Windows, puis tape « powershell ») et colle ces commandes **une par une**, en appuyant sur Entrée entre chaque.

```
winget install Python.Python.3.12
```

```
pip install yt-dlp faster-whisper gallery-dl
```

```
winget install Gyan.FFmpeg
```

**Ferme complètement PowerShell et rouvre-le.** Sans ça, Windows ne trouvera pas les nouveaux programmes.

Vérifie que tout répond :

```
python -m yt_dlp --version
```

```
ffmpeg -version
```

Le premier affiche une date, le second un pavé de texte. Si l'un des deux dit « commande introuvable », l'installation n'est pas passée — reprends-la avant d'aller plus loin.

> **Des lignes rouges pendant l'installation ?** C'est normal. `pip` signale souvent des conflits de version avec d'autres programmes déjà présents. Tant que tu lis `Successfully installed` à la fin, tout va bien.

---

## Étape 2 — Se connecter à Instagram… avec Firefox

C'est **le** piège du projet, et celui qui fait abandonner la plupart des gens.

Instagram refuse de montrer ses Reels à qui n'est pas connecté. Le script doit donc emprunter le cookie de connexion de ton navigateur. Sauf que **depuis Chrome 127, Google chiffre ses cookies d'une façon que yt-dlp ne sait pas déchiffrer sous Windows**. Tu obtiens l'erreur `Failed to decrypt with DPAPI`, et absolument rien ne fonctionne.

Il n'y a pas de contournement côté Chrome. La solution :

```
winget install Mozilla.Firefox
```

Ouvre Firefox, va sur **instagram.com**, connecte-toi, puis **ferme complètement Firefox** — Windows verrouille le fichier de cookies tant que le navigateur tourne.

Teste avant d'aller plus loin :

```
python -m yt_dlp --cookies-from-browser firefox --dump-json --skip-download "https://www.instagram.com/reel/UN_REEL_QUELCONQUE/"
```

Un déluge de texte illisible = c'est gagné. Une erreur = inutile de continuer, il faut régler ça d'abord.

---

## Étape 3 — Récupérer ses liens

Sur Instagram : **Paramètres → Centre des comptes → Vos informations et autorisations → Télécharger vos informations**. Choisis le format **JSON**. Le fichier arrive par mail, parfois sous 48 h.

Dézippe-le, puis **déplace le dossier dans un endroit sûr** — pas dans Téléchargements, où il finira par disparaître.

Deux fichiers t'intéressent :

- `saved_posts.json` — tes posts sauvegardés
- `saved_collections.json` — tes posts rangés en collections

> Traite les deux, l'un après l'autre. Le script tient un journal et ne retraitera jamais deux fois la même vidéo.

---

## Étape 3 bis — Choisir ce qu'on ingère vraiment

Un export, c'est tout : les pépites comme les mèmes. Transcrire des centaines de Reels inutiles, c'est des heures de calcul pour des fiches que personne ne relira jamais.

**`filtre.py`** lit l'export, applique **`filtres.yaml`** (un fichier que tu édites à la main) et écrit la liste des liens retenus. Il génère aussi ton fichier de départ, à partir de tes propres collections :

```
python filtre.py --export "CHEMIN\VERS\instagram-votre_compte" --init-config
```

```
python filtre.py --export "CHEMIN\VERS\instagram-votre_compte" --liste-collections
```

```
python filtre.py --export "CHEMIN\VERS\instagram-votre_compte" --rapport
```

```
python ingest.py liens-filtres.txt
```

Dans `filtres.yaml` (que tu ne versionnes pas : il décrit tes collections), chaque collection de ton compte a sa ligne :

```yaml
  - nom: "Cuisine"
    garder: true
  - nom: "Mèmes"
    garder: false
```

Passe une collection à `garder: true` et elle entre dans la base ; à `false` et elle reste dehors. Les `themes` (mots-clés) servent de filet pour les posts qu'aucune collection ne couvre. Un exemple complet est fourni dans `filtres.exemple.yaml`, et il faut PyYAML (`pip install pyyaml`).

Deux choses valent la peine d'être sues :

- L'export Instagram **relie bien chaque post à sa collection**, mais pas là où on l'attend : la clé `media` est toujours vide, le lien se cache dans une entrée `label_values` sans libellé. `filtre.py` va le chercher : le tri par collection est donc **exact**, pas deviné.
- Les collections ne sont pas toujours rangées comme leur nom le laisse croire — un post peut atterrir dans une collection voisine. Le rapport affiche un exemple de légende par thème : jette un œil avant de lancer une longue session de transcription.

---

## Étape 4 — Le script de récolte

Crée un dossier `Vault` dans ton profil utilisateur et places-y **`ingest.py`** (lien de téléchargement en bas de page).

Ce qu'il fait, pour chaque lien :

1. récupère la description et les métadonnées via **yt-dlp**
2. télécharge l'audio et le transcrit en local avec **Whisper**
3. extrait trois images de la vidéo
4. écrit une fiche Markdown dans `raw/`
5. note son avancement, pour pouvoir reprendre après une interruption

**Commence par 10 vidéos**, jamais par la totalité :

```
cd "$HOME\Vault"
```

```
python ingest.py "CHEMIN\VERS\saved_posts.json" --vault "$HOME\Vault" --cookies firefox --limite 10
```

Le premier lancement télécharge le modèle Whisper (500 Mo, une seule fois) — plusieurs minutes de silence, c'est normal.

Ouvre ensuite une fiche dans `raw/` et **lis la section « Transcription audio »**. C'est le moment décisif :

- **Elle est riche et cohérente** → parfait, tout le contenu utile est dans le texte. Tu peux lancer la totalité.
- **Elle est vide ou incompréhensible** → tes Reels sont sur musique de fond, l'information est dans le texte incrusté à l'image. Il faudra faire lire les images extraites par Claude, ce qui consomme beaucoup plus de quota.

Si le test est concluant, lance tout en retirant `--limite 10`. Compter **20 à 40 secondes par vidéo** : environ 1 h 30 pour 200 Reels. Tu peux couper avec `Ctrl+C` et relancer la même commande plus tard, il reprend où il s'était arrêté.

> **Beaucoup d'échecs sur des liens en `/p/` ?** Ce sont des carrousels (des photos, pas des vidéos) : `gallery-dl` prend le relais, à condition de l'avoir installé à l'étape 1. Et si tes liens en échec commencent par `B5`, `B6`… ce sont des posts de 2019-2020, souvent supprimés depuis. Irrécupérables, et sans grande valeur.

---

## Étape 5 — Transformer les transcriptions en index

À ce stade, tu as des centaines de fiches brutes, longues et brouillonnes. Il faut les condenser.

Installe **Claude Desktop** depuis **claude.ai/download** — surtout pas depuis le Microsoft Store, les versions tierces ne gèrent pas Cowork. Sous Windows, Cowork exige la fonctionnalité « Plateforme de machine virtuelle » ; si l'application refuse de l'ouvrir, lance ceci dans un terminal **administrateur**, puis redémarre :

```
Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All
```

Ouvre Cowork, donne-lui le dossier `Vault`, et colle cette consigne :

```
Tu travailles dans le dossier de mon Vault.

Objectif : construire un index consultable de mes vidéos Instagram sauvegardées.

Le dossier `raw` contient une fiche .md par vidéo, avec sa source, son auteur,
sa description et la transcription de l'audio.

Travaille par lots de 25 fiches. Pour chaque fiche :
- résume en 2 à 4 lignes ce qu'elle apporte CONCRÈTEMENT : les noms cités, les
  titres recommandés, les lieux, les adresses, les prix, les horaires. Surtout
  pas de généralité du type "cette vidéo parle de productivité".
- attribue 1 à 3 thèmes. N'utilise aucune liste prédéfinie : déduis les thèmes
  de ce que tu lis, et réutilise les mêmes libellés d'une fiche à l'autre pour
  rester cohérent.
- si une fiche est trop pauvre pour un vrai résumé, signale-le plutôt que
  d'inventer.

Écris le résultat dans `index.md`, en AJOUTANT à la fin à chaque lot (ne réécris
jamais le fichier entier). Format de chaque entrée :

### <titre court et parlant>
- lien : <url>
- auteur : <auteur>
- thèmes : <thème1>, <thème2>
- contenu : <le résumé concret>

Tiens à jour un fichier `progression.txt` avec le nom de la dernière fiche
traitée, pour pouvoir reprendre plus tard.

Fais le premier lot de 25, puis arrête-toi et dis-moi combien il en reste.
```

Réponds ensuite **« continue »** à la fin de chaque lot. Compter une dizaine de lots pour 250 fiches. Si tu atteins ta limite d'usage, attends la réouverture et écris « reprends l'indexation à partir de progression.txt ».

**Pourquoi un index plutôt que d'interroger les fiches brutes ?** Parce que faire relire 250 fiches complètes à chaque question épuiserait ton quota en trois requêtes. L'index tient dans une fraction de la place et suffit à 90 % des questions.

**Ne décide pas des catégories à l'avance.** En laissant Claude les déduire, tu découvres ce que tu sauvegardes réellement — et c'est rarement ce qu'on croit.

---

## Étape 6 — Une page pour consulter

Demande à Cowork :

```
Crée une page `vault.html` à la racine du Vault. Contraintes :

- un seul fichier, sans dépendance externe, qui marche hors ligne
- les données lues depuis index.md et intégrées directement dans le fichier
- une barre de recherche qui filtre en direct sur le titre, le contenu et l'auteur
- des boutons de filtre par thème, avec le nombre d'entrées sur chacun
- chaque entrée affiche titre, thèmes, résumé, et un lien cliquable vers le post
- lisible sur téléphone aussi

Crée aussi le script qui régénère cette page à partir de index.md.
```

Tu obtiens une page à ouvrir d'un double-clic : recherche instantanée, filtres, hors ligne, et **sans consommer une miette de quota**. C'est ce que tu utiliseras au quotidien.

---

## Étape 7 — Envoyer une vidéo depuis son téléphone

L'idée : ton téléphone dépose le lien dans un fichier, ton PC le traite tout seul.

### Le raccourci iPhone

Dans l'app **Raccourcis** :

1. **+** en haut à droite
2. Ajoute l'action **« Ajouter au fichier texte »**
3. Dans le champ à côté de « Ajouter à la suite », choisis la variable **Entrée du raccourci**
4. Dans **« Chemin du fichier »**, tape simplement `inbox.txt`
5. Active **« Créer une nouvelle ligne »**
6. Ouvre les détails (icône **ⓘ**) → active **« Afficher dans la feuille de partage »**, et limite les types acceptés à **URL**
7. Nomme-le **« Envoyer au Vault »**

À l'usage : sur un Reel, **Partager → Plus → Envoyer au Vault**. Ça marche aussi depuis TikTok, sans rien changer.

> **N'essaie pas de passer par OneDrive.** Il se monte en **lecture seule** dans le sélecteur de fichiers d'iOS : le raccourci ne pourra jamais y écrire. Laisse le réglage sur `Shortcuts` et tape juste `inbox.txt` — le fichier se crée tout seul dans iCloud au premier envoi.

### Côté PC

Installe iCloud pour Windows :

```
winget install Apple.iCloud
```

**Ouvre l'application, connecte-toi avec ton identifiant Apple, et coche iCloud Drive.** L'installation seule ne suffit pas.

Fais un premier envoi depuis ton téléphone, puis localise le fichier :

```
Get-ChildItem "$HOME\iCloudDrive" -Recurse -Filter "inbox.txt" | Select-Object FullName
```

Le chemin ressemblera à `...\iCloudDrive\iCloud~is~workflow~my~workflows\inbox.txt`.

> **Fais un clic droit sur ce fichier → « Toujours conserver sur cet appareil ».** Sans ça, iCloud le garde dans le nuage et ton script lira une version périmée. C'est la cause n°1 des « ça ne marche plus ».

### L'automatisation

Crée le lanceur. **Remplace `CHEMIN_TROUVE_CI_DESSUS` par le chemin exact que la commande précédente t'a affiché**, puis colle le tout en une seule ligne :

```
Set-Content "$HOME\Vault\inbox.bat" -Encoding ASCII -Value '@echo off', 'cd /d "%USERPROFILE%\Vault"', 'python ingest.py "CHEMIN_TROUVE_CI_DESSUS" --vault "%USERPROFILE%\Vault" --cookies firefox'
```

Vérifie le résultat avant de continuer :

```
Get-Content "$HOME\Vault\inbox.bat"
```

Puis programme-le, une ligne à la fois :

```
$a = New-ScheduledTaskAction -Execute "$HOME\Vault\inbox.bat"
```

```
$t = New-ScheduledTaskTrigger -Daily -At 6pm
```

```
Register-ScheduledTask -TaskName "Vault Inbox" -Action $a -Trigger $t
```

```
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun
```

```
Set-ScheduledTask -TaskName "Vault Inbox" -Settings $s
```

`StartWhenAvailable` rattrape les exécutions manquées : si ton PC est éteint à l'heure dite, la tâche se lancera au prochain démarrage.

Dernière brique, dans Cowork :

```
Crée une tâche récurrente quotidienne à 8h :
1. lire le dossier raw et repérer les fiches absentes de index.md
2. les résumer avec le même format et le même vocabulaire de thèmes
3. les ajouter à la fin de index.md, mettre à jour progression.txt
4. régénérer vault.html
S'il n'y a aucune nouvelle fiche, ne rien faire.
```

**La boucle est bouclée** : tu partages depuis ton lit, ton PC transcrit le soir, l'index se met à jour le lendemain matin.

---

## Étape 8 — Interroger sa base

Crée un fichier `CLAUDE.md` à la racine du Vault. Cowork le lit automatiquement au début de chaque session :

```
- Ce dossier est une base de vidéos Instagram et TikTok que j'ai sauvegardées.
- Pour toute question sur mon contenu, commence TOUJOURS par lire index.md.
- N'ouvre les fiches complètes de raw/ que si index.md ne suffit pas.
- Ne mentionne jamais un lieu, un titre ou un conseil qui ne vient pas de mes
  fiches. Si je n'ai rien sur un sujet, dis-le simplement.
- Après toute modification de index.md, régénère vault.html.
- Réponds en français.
```

Tu peux alors demander directement :

> Je pars 7 jours au Japon. Organise-moi un circuit avec tout ce que j'ai gardé — lieux, restaurants, adresses. Dis-moi ce que tu écartes et pourquoi.

> Qu'est-ce que je sauvegarde le plus sans jamais l'utiliser ?

> Ressors-moi les pâtisseries parisiennes, classées par arrondissement.

**Depuis ton téléphone** : dépose `index.md` et `vault.html` sur Google Drive, et interroge-les depuis l'app Claude avec le connecteur Drive activé.

---

## Bonus — La carte du graphe

C'est l'image qui a fait circuler le projet : un nuage de points reliés, chaque point une vidéo, chaque gros nœud un thème.

Installe **Obsidian** (gratuit) :

```
winget install Obsidian.Obsidian
```

Au lancement, choisis **« Ouvrir un dossier comme coffre »** et sélectionne ton `Vault` — pas le coffre de démonstration créé par défaut.

Lance ensuite **`graphe.py`** (lien en bas de page), qui ajoute les liens `[[Thème]]` à chaque fiche et crée une note par thème :

```
python graphe.py --vault "$HOME\Vault"
```

Puis `Ctrl+G` dans Obsidian. Monte **Repel** dans les réglages « Forces » pour aérer, et crée un groupe `path:themes` en couleur vive pour faire ressortir les thèmes.

**Sois honnête avec toi-même** : c'est superbe, ça fait un excellent visuel, mais on s'en sert peu au quotidien. La page `vault.html` avec ses filtres est bien plus efficace pour retrouver quelque chose. Le graphe, c'est l'affiche du projet.

---

## Les cinq pièges, résumés

1. **Chrome ne fonctionne pas.** Erreur `DPAPI`, sans solution. Installe Firefox.
2. **Ferme ton navigateur** avant de lancer le script. Windows verrouille le fichier de cookies.
3. **OneDrive est en lecture seule** sur iPhone. Passe par iCloud pour la boîte de réception.
4. **iCloud ne télécharge pas les fichiers tout seul.** Clic droit → « Toujours conserver sur cet appareil ».
5. **Range ton export ailleurs que dans Téléchargements.** Il finira supprimé au pire moment.

---

## Les limites, en toute franchise

**Ce n'est pas de la magie, c'est de la transcription.** Si tes Reels n'ont pas de voix off, il n'y aura pas grand-chose à extraire — vérifie sur 10 vidéos avant d'en lancer 300.

**Les quotas existent.** Ce montage tient sur un abonnement Pro parce qu'on ne fait relire que l'index. Traiter des dizaines de milliers de vidéos demanderait une clé API et un vrai budget.

**Instagram limite le rythme.** Sur plusieurs centaines de posts, mieux vaut procéder par lots de 50 étalés sur quelques jours.

**Une partie du contenu est irrécupérable.** Les posts supprimés ou passés en privé depuis que tu les avais enregistrés sont perdus. Sur un stock ancien, compte facilement un tiers de pertes.

---

## Les fichiers

- **`ingest.py`** — récolte et transcription
- **`graphe.py`** — création des liens pour Obsidian

*Tuto rédigé après un montage réel, du premier `winget install` jusqu'au circuit de voyage. Les pièges décrits ont tous été rencontrés pour de vrai.*
