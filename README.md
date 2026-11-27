# JOAL Smart Controller

Projet étudiant / proof of concept : un contrôleur web qui pilote le moteur [JOAL](https://github.com/anthonyraymond/joal) selon des règles par tracker. Il s'appuie sur l'état de qBittorrent et sur des relevés *scrape* indépendants.

> ⚠️ La plupart des trackers privés interdisent la simulation d'upload : un compte peut être banni. Pour une démonstration, utiliser un tracker de test local (ex. opentracker). Par défaut, `dryRun` est activé : aucune annonce n'est envoyée.

Version : voir `app/version.py`. Historique : [CHANGELOG.md](CHANGELOG.md).

## Principe

- **Sécurité d'abord.** Si l'état est inconnu (qBittorrent injoignable, scrape absent ou périmé), le contrôleur n'autorise rien : une valeur inconnue n'est jamais traitée comme 0.
- **qBittorrent prioritaire.** JOAL ne simule qu'un torrent **complet et arrêté** dans qBittorrent. Une garde rapide (toutes les 5 s) arrête JOAL dès que le torrent reprend ou que l'API devient injoignable.
- **Mode « JOAL uniquement ».** Des fichiers `.torrent` importés dans `/data/torrents` peuvent être annoncés sans qBittorrent.
- **Aucune URL ni passkey** n'est exposée par l'API ou la WebUI : seuls les domaines sont affichés.
- **Dossier surveillé** : déposer des `.torrent` dans `data/import/` suffit à les importer.
- **Plages horaires** par règle, **page Santé**, **graphique seeders / leechers** par torrent, **motif de refus** des annonces.
- **Deux designs** au choix dans *Paramètres → Apparence* : « Classique » (sombre) et « Baie » (clair). Le choix est enregistré dans `data/interface.json` et s'applique aussi à la page de connexion.

## Architecture

| Fichier | Rôle |
|---|---|
| `app/main.py` | Serveur HTTP, boucles de surveillance (poll, garde, nettoyage, scrape, JOAL uniquement), lancement des processus JOAL |
| `app/core.py` | Validation de `policy.json`, bencode, filtrage des trackers à infohash constant |
| `app/qbit.py`, `app/qbit_settings.py` | Client API qBittorrent (4.x et 5.x), connexion modifiable dans la WebUI |
| `app/tracker_scrape.py`, `app/swarm_monitor.py` | Scrapes HTTP(S)/UDP en lecture seule et historique des leechers |
| `app/handoff.py` | Basculement qBittorrent ↔ JOAL avec confirmations et temporisation |
| `app/cleanup.py` | Suppression optionnelle des torrents sans leechers (désactivée par défaut) |
| `app/solo_torrents.py`, `app/seed_presence.py` | Bibliothèque « JOAL uniquement », présence à débit 0 |
| `app/web_auth.py` | Sessions par cookie, CSRF, limitation des tentatives |
| `app/import_watch.py` | Dossier surveillé `data/import/` |
| `app/log_rotation.py` | Rotation par taille des journaux JOAL |
| `testbed/` | Tracker de test, générateur de torrents, banc d'essai (`compose.test.yaml`) |
| `app/backup_bundle.py` | Sauvegarde et restauration ZIP validées de `/data` |
| `app/interface_design.py`, `app/static/design-baie*.css` | Choix du design de la WebUI ; polices Barlow auto-hébergées (`app/static/fonts/`, licence OFL) |
| `telemetry/UploadTelemetry.java` | Greffon JOAL qui publie débits et compteurs (lu par le contrôleur) |

Chaque processus JOAL correspond à une règle, car les débits JOAL sont globaux à un processus.

## Installation (Unraid)

1. Copier le projet dans `/mnt/user/appdata/joal-smart-controller`.
2. Construire l'image :
   ```sh
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```
3. Importer `unraid/my-joal-smart-controller.xml` (ou utiliser `docker compose up -d` avec un `.env` copié depuis `.env.example`).
4. Définir `ADMIN_PASSWORD` (12 caractères minimum) ou `ADMIN_PASSWORD_HASH`. Copier les profils `.client` de JOAL dans `data/clients/`.
5. Ouvrir la WebUI (utilisateur `admin`) et configurer qBittorrent et les règles. La connexion qBittorrent s'applique immédiatement.

**Mise à jour** : conserver `data/` et `.env`, remplacer le code, puis lancer `unraid/update-joal.sh` dans **User Scripts** (options modifiables en haut du script). Le script construit l'image et met à jour les capacités du template. Il supprime aussi, si les options sont à `yes` : les fichiers d'anciennes versions absents du zip (`CLEAN_OLD_FILES`, jamais `data/` ni `.env`), les anciennes images de l'application (`CLEAN_OLD_IMAGES`) et le cache de build inutilisé depuis 7 jours (`CLEAN_BUILD_CACHE`). Terminer par **Docker → Edit → Apply**.

## Mises à jour automatiques (GitHub + bouton « Update » d'Unraid)

Unraid ne propose « update ready » que pour une image publiée sur un registre. GitHub construit donc l'image (`.github/workflows/image.yml`) :

- à chaque envoi sur `main`, et **chaque lundi** : dernière version de JOAL sur Docker Hub, dernières mises à jour de Java 21, derniers profils `.client` du dépôt JOAL (ajoutés là-bas entre deux versions) ;
- tests unitaires, puis **essai de bout en bout** (`ci/smoke_image.py`) : JOAL annonce un torrent de test à un faux tracker avec un vrai profil, la télémétrie doit remonter les seeders/leechers, puis le contrôleur doit démarrer et copier les profils. Un JOAL qui casse le correctif de télémétrie n'est **jamais publié** (e-mail d'échec de GitHub) ;
- publication sur `ghcr.io/<compte>/joal-smart-controller:latest` (et `<version>-joal<version JOAL>`), seulement si quelque chose a changé.

Au démarrage, le conteneur ajoute dans `/data/clients` les profils `.client` qui y manquent ; un profil existant, même modifié, n'est jamais remplacé. La version de JOAL est affichée en pied de page.

**Mise en place (une fois)**

1. Créer un dépôt **privé** sur GitHub et y envoyer le contenu du zip, dossier `.github/` compris :
   `git init && git add . && git commit -m "JOAL Smart Controller" && git branch -M main && git remote add origin https://github.com/<compte>/joal-smart-controller.git && git push -u origin main`
2. Onglet **Actions** : le workflow « Image Docker » démarre (environ 10 min). Il crée le paquet `joal-smart-controller` (onglet **Packages** du compte).
3. Paquet → *Package settings* → *Change visibility* → **Public**. Le code du dépôt reste privé ; l'image, elle, devient téléchargeable par qui connaît son nom (elle ne contient aucun secret : mots de passe, passkeys et `.torrent` restent dans `/data`). C'est ce qui permet au bouton « Update » d'Unraid de fonctionner : avec une image privée, Unraid affiche souvent « not available » même après `docker login`.
4. Unraid → Docker → joal-smart-controller → **Edit** → *Repository* : `ghcr.io/<compte>/joal-smart-controller:latest` → **Apply**. `/data` est conservé.
5. Ensuite : Unraid affiche « update ready » quand GitHub a publié une version testée. Le plugin *CA Auto Update Applications* peut l'appliquer automatiquement.

**Image privée** (si l'image ne doit pas être publique) : `unraid/ghcr-login.sh` dans User Scripts, planifié « At Startup of Array » (jeton GitHub avec le seul droit `read:packages`). L'image se télécharge, mais le bouton peut rester sur « not available » : utiliser alors *Force update* (mode avancé de l'onglet Docker).

Le mode local (`update-joal.sh`, image `joal-smart-controller:local`) reste disponible ; il utilise les versions épinglées du `Dockerfile`.

## Alertes, statistiques et nouveautés (v1.31)

- **Alertes sur le téléphone** : *Paramètres → Alertes sur le téléphone*. Choisir ntfy (adresse du sujet, jeton facultatif), Gotify (adresse du serveur + jeton d'application) ou Discord (URL du webhook), puis « Envoyer une alerte de test ». « Les problèmes seulement » envoie les incidents ; « Tout » ajoute les rétablissements et les mises à jour installées. Le jeton reste dans `/data/alerts.json` et n'est jamais inclus dans une sauvegarde.
- **Statistiques** : upload simulé par tracker (aujourd'hui, 7 et 30 jours), temps de seed, nombre de torrents et graphique des 30 derniers jours. Conservé 90 jours dans `/data/tracker-stats.json`.
- **Nouveautés** : après une mise à jour, une fenêtre résume les changements ; lien « Nouveautés » en pied de page.
- **Sauvegarde sans passkeys** : décocher « Inclure les fichiers .torrent » avant l'export (v1.35 : toutes les adresses de tracker y sont réduites au nom du serveur).

## Discrétion vis-à-vis des trackers (v1.33)

- Un torrent qui entre ou sort d'une session est ajouté ou retiré **à chaud** : JOAL n'est plus relancé, les autres torrents ne réannoncent pas. Un seul JOAL par règle (torrents qBittorrent et importés).
- Relevés : rien pour un torrent déjà annoncé par JOAL ; plusieurs torrents d'un même tracker en une requête ; requêtes espacées et non périodiques ; un torrent resté sans leecher est relevé de moins en moins souvent (Paramètres → Relevés indépendants → « Intervalle max. sans leecher », 240 min par défaut).
- Les relevés du contrôleur après une session reprennent l'identité (peer_id, clé, port) que JOAL utilisait.
- La page Règles signale un profil `.client` différent du qBittorrent réel et propose le bon.
- v1.34 : JOAL annonce le port d'écoute du vrai qBittorrent (Paramètres → « Annoncer le port de qBittorrent », activé par défaut) et n'interroge plus aucun site d'adresse IP publique. Les torrents qui partagent dans qBittorrent ne sont plus relevés (sauf basculement automatique activé).

## Sécurité du compte et widget (v1.32)

- **Mot de passe** : modifiable dans Paramètres → Sécurité. Il est enregistré haché (scrypt) dans `/data/admin-password.json` et devient prioritaire sur Docker. L'interface affiche le hash à reporter dans `ADMIN_PASSWORD_HASH` si on veut retirer le mot de passe en clair de Docker → Edit. Changer le mot de passe ferme les autres sessions.
- **Déconnecter les autres sessions** : bouton dans Paramètres → Sécurité.
- **Double authentification** (facultative) : clé à ajouter dans Aegis, 2FAS, Google Authenticator… puis code à 6 chiffres à chaque connexion. Téléphone perdu : `DISABLE_TOTP=1`, redémarrer, désactiver dans Paramètres, remettre `0`.
- **Plafond quotidien par règle** : champ « Plafond d'upload simulé par jour » dans l'éditeur de règles. Une fois atteint, les sessions de la règle s'arrêtent jusqu'à minuit (heure du conteneur). Les torrents en Forced ne sont pas plafonnés.
- **Export CSV** des statistiques (page Statistiques, 90 jours, séparateur `;`, ouvre directement dans Excel).
- **Widget** (Homepage, Homarr, Home Assistant) : créer un jeton dans Paramètres → Widget. Il ne donne accès qu'à `GET /api/widget` (débit, volume du jour, sessions, état qBittorrent, versions). Exemple Homepage :

```yaml
- JOAL Smart:
    href: http://IP-UNRAID:8788
    widget:
      type: customapi
      url: http://IP-UNRAID:8788/api/widget
      headers:
        Authorization: Bearer jsw_…
      mappings:
        - field: uploadKiBps
          label: Débit
          suffix: " Kio/s"
        - field: uploadedTodayGiB
          label: Aujourd’hui
          suffix: " Gio"
        - field: activeTorrents
          label: Torrents
```

- **Trackers** : les relevés (scrapes) envoient les mêmes en-têtes HTTP que le profil `.client` de JOAL, se connectent à l'adresse IP déjà vérifiée (protection DNS rebinding), et les URL des trackers (passkeys) sont masquées dans `joal.log`.

## Variables principales

| Variable | Défaut | Rôle |
|---|---|---|
| `ADMIN_PASSWORD` | — | Mot de passe de la WebUI (12 caractères minimum) |
| `ADMIN_PASSWORD_HASH` | vide | Hash scrypt à la place du mot de passe en clair (prioritaire sur `ADMIN_PASSWORD`) |
| `DISABLE_TOTP` | `0` | `1` coupe la double authentification (récupération) |
| `ALLOW_ROOT` | `0` | Dépannage seulement : `1` laisse démarrer en root si le changement d'utilisateur échoue |
| `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD` | vide | Valeurs par défaut de la connexion (modifiables dans la WebUI) |
| `PUID`, `PGID` | `99`, `100` | Utilisateur d'exécution (Unraid `nobody:users`) |
| `TORRENT_FILE_MODE`, `TORRENT_DIR_MODE` | `0660`, `0770` | Permissions de `/data/torrents` |
| `COOKIE_SECURE` | `0` | `1` uniquement derrière HTTPS |
| `TRUSTED_PROXIES` | vide | IP du reverse proxy autorisé à fournir `X-Forwarded-For` |
| `IMPORT_WATCH` | `1` | Dossier surveillé `data/import/` (0 pour désactiver) |
| `LOG_MAX_MB`, `LOG_BACKUPS` | `5`, `3` | Rotation des journaux JOAL |
| `TZ` | fourni par Unraid | Fuseau horaire des plages horaires |
| `JOAL_XMX` | `128m` | Mémoire minimale de chaque JVM JOAL (une par règle active) ; augmentée automatiquement selon la taille des `.torrent` de la session, jusqu'à 1536m |

## Sécurité

- Conteneur non-root, `no-new-privileges`, capacités limitées au démarrage (attribution de `/data`).
- Cookies `HttpOnly` et `SameSite=Strict`, jeton CSRF, contrôle d'origine, CSP stricte, aucun `innerHTML` côté client.
- Fichiers d'état écrits de façon atomique en `0600`.
- L'interface est en HTTP : **ne pas l'exposer sur Internet** sans reverse proxy TLS ni contrôle d'accès.

## Tests

```sh
cd tests
python3 -m unittest discover -s . -p "test_*.py"   # tests unitaires et HTTP
node test_table_sort.cjs                           # tri du tableau (JS)
```

Les scripts `browser_*.py` nécessitent Playwright et Chromium (ou Chrome via `CHROMIUM`) :

```sh
cd tests
for f in browser_*.py; do CHROMIUM=/usr/bin/google-chrome python3 "$f" || echo "ÉCHEC $f"; done
```

**Banc d'essai** (aucun tracker réel) : voir [testbed/README.md](testbed/README.md). Test de bout en bout avec le vrai JOAL :

```sh
JOAL_JAR=/chemin/joal.jar JOAL_CLIENTS=/chemin/clients python3 tests/e2e_testbed.py
```

## Limites connues

- JOAL ne simule aucun upload si le tracker ne signale aucun leecher, ou aucun autre seeder que lui. La ligne du torrent l'indique (« Upload à 0 : … »).
- Les nombres de leechers dépendent des réponses des trackers ; une couverture partielle n'est jamais présentée comme le swarm entier.
- Le greffon de télémétrie lit un champ privé de JOAL 2.1.37 par réflexion. En cas d'autre version de JOAL, JOAL refuse de démarrer (échec explicite au lancement).
- La restauration remplace les fichiers présents dans l'archive. Elle ne supprime pas les `.torrent` absents de l'archive.
