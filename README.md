# IMV — Incident Manager Virtuel (prototype)

Prototype de l'Étape 0 (voir `PLAN.md` et `ETUDE_FAISABILITE.md`) : un agent qui **rejoint une réunion Teams par son URL, comme invité**, via un navigateur automatisé (Playwright), puis **lit le chat** en continu. Aucune API Graph ni app Teams n'est nécessaire.

## Installation

Python 3.11 ou plus. Deux façons équivalentes :

**Avec pip seul** (ex. poste du bureau, sans uv) :

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # requirements.txt sans les tests
.venv/bin/python -m playwright install chromium
```

**Avec uv** :

```bash
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
.venv/bin/playwright install chromium
```

Si `playwright install chromium` échoue (proxy ou filtrage du réseau d'entreprise), inutile d'insister : l'agent utilise alors **Google Chrome ou Microsoft Edge déjà installés** (essayés dans cet ordre si le Chromium de Playwright est absent). Pour imposer un navigateur : `--browser chrome|msedge|chromium`, ou `IMV_BROWSER=msedge` dans `.env`. Dans tous les cas un profil temporaire vierge est utilisé, jamais votre profil personnel. Pour retenter le téléchargement derrière un proxy : `HTTPS_PROXY=http://proxy:port python -m playwright install chromium`. Pour installer Chromium à la main depuis un navigateur : voir [INSTALL_CHROMIUM_MANUEL.md](INSTALL_CHROMIUM_MANUEL.md).

`pyproject.toml` fait référence pour les dépendances ; `requirements.txt` et `requirements-dev.txt` doivent rester identiques, ce que vérifie `tests/test_requirements.py`. Après l'ajout d'une dépendance, mettre à jour les deux.

L'agent utilise un profil temporaire neuf et rejoint dans le navigateur, pas dans l'application Teams.
Le lanceur Teams est contourné vers sa destination web lorsqu'elle est fournie ; sinon,
`msLaunch=false` et `suppressPrompt=true` sont ajoutés. La résolution du lien et sa
destination utilisent aussi `webjoin=true` pour demander explicitement le parcours web.
Il ne faut pas autoriser l'ouverture
de l'application Teams. Si la connexion bloque, ajoutez `--show --browser msedge` à la commande
`serve` ou `join` pour afficher Edge. Un choix « Toujours autoriser » n'est pas conservé entre sessions.

## Utilisation

```bash
.venv/bin/python imo_agent.py join "<url de la réunion>"
```

1. Un navigateur Chromium s'ouvre et rejoint la réunion sous le nom « IMV - Oscar », sans caméra ni micro. « Oscar » se dit et se transcrit de la même façon en français et en anglais : on interpelle l'agent par ce nom, à l'oral (« Oscar, un résumé ») comme dans le chat (« Oscar, explique-moi… »). `--name` ou `IMV_NAME` dans `.env` impose un autre nom ; le mot après « IMV - » sert alors à l'interpeller. Pour plusieurs agents dans une même réunion, donnez-leur des noms différents.
2. Un participant doit **l'admettre depuis la salle d'attente**.
3. L'agent ouvre le chat et y poste un **message d'accueil** qui annonce l'enregistrement (si le chat est ouvert aux invités).
4. Chaque message s'affiche dans la console et est enregistré dans la base SQLite `sessions/imv.db`.
5. `Ctrl+C` pour quitter la réunion.

Options : `--captions` (transcription de la voix, voir ci-dessous), `--captions-language fr|en` (langue de la réunion : langue parlée des sous-titres Teams, et langue des messages de l'agent et des analyses ; défaut : sous-titres inchangés, messages en français ; l'interface web propose l'anglais par défaut), `--context FICHIER` (défaut : `contexte.md` du répertoire courant s'il existe), `--im NOM` (responsable d'IMV, voir « Qui peut utiliser les commandes »), `--name` (nom affiché : lettres, chiffres, espaces et `- ' . _ @` uniquement), `--incident INC…`, `--no-welcome`, `--avatar [IMAGE]` (caméra allumée avec le robot IMV, ou votre image PNG/JPEG : un invité n'a pas de photo de profil), `--db`, `--out` (copie JSONL), `--show` (affiche la fenêtre du navigateur, cachée par défaut : utile pour diagnostiquer), `--admission-timeout`, `--debug-dir`, `-v`.

### Voix : sous-titres en direct (prototype)

Avec `--captions`, l'agent active les **sous-titres en direct de Teams** (menu « More » → « Captions » pour un invité, ou « Language and speech » → « Turn on live captions ») et lit le texte affiché : chaque phrase est enregistrée avec le nom de la personne qui parle, comme un message `transcript`. Teams fait la transcription : aucun flux audio n'est traité par l'agent et la voix ne part vers aucun service supplémentaire.

- Les phrases s'affichent dans la console avec la mention « (oral) », et les analyses (`imv résumé`, `imv pistes`…) en tiennent compte en les distinguant du chat écrit.
- Le message d'accueil annonce la transcription ; `imv pause` la suspend aussi.
- **Commandes vocales** : l'agent n'agit sur ce qui est dit que s'il est interpellé, par son nom (plus facile à dire en anglais) ou par « IMV » : « Oscar, give me a summary please », « Oscar, quelles sont les pistes ? ». L'appel peut être seul sur une ligne (« IMV. »), la demande dans la phrase suivante (15 s maximum). Les sous-titres écrivent souvent « IMV » en « MV » : c'est reconnu. La demande est convertie en commande (résumé, catchup, pistes, questions, timeline, actions, com, rapport, clore, pause, aide) ; sinon, avec `--llm`, elle est traitée comme une question libre (`imv ?`), et pour un responsable comme une conversation libre avec l'assistant (contexte chargé, échanges précédents). Dans le chat, « Oscar, … », « Oscar : … » ou une mention « @IMV - Oscar » valent « imv … ». L'agent répond dans le chat, en rappelant la phrase comprise. Les droits sont les mêmes qu'à l'écrit (le nom de l'orateur vient de Teams) ; `autoriser` et `retirer` ne se disent pas à l'oral, et en pause l'agent n'écoute plus (`imv reprise` se tape dans le chat).
- Si les sous-titres ne peuvent pas être activés (option absente pour les invités, interface différente), l'agent continue avec le chat seul et le signale dans la console.
- **Prototype** : les sélecteurs de l'interface viennent de sources publiques et doivent être confirmés sur le vrai Teams. Chaque étape est capturée dans `debug/` (`captions-menu`, `captions-on`, `captions`, `captions-failed`) : envoyez ces fichiers pour ajuster la lecture. La langue parlée des sous-titres doit correspondre à celle de la réunion (réglage « Spoken language » de Teams) : une phrase en français transcrite en anglais devient inexploitable.
- Transcrire la voix est un traitement de données personnelles : à valider avec le RSSI (et le CSE) avant tout usage réel.

```bash
python imo_agent.py join "<url>" --captions --llm azure-openai
```

### Commandes dans le chat

Un invité ne peut pas être @mentionné : les commandes commencent par `imv`.

| Commande | Effet |
| --- | --- |
| `imv aide` | Liste des commandes |
| `imv démarrer [ID incident]` | Démarre le suivi de l'incident ; les messages déjà captés y sont rattachés |
| `imv statut` | Incident suivi, état de l'enregistrement, nombre de messages |
| `imv pause` / `imv reprise` | Suspend / reprend l'enregistrement du chat |

| `imv résumé` | Point de situation : symptômes, impact, hypothèses, actions, prochaine étape |
| `imv catchup` | Résumé court pour un nouvel arrivant |
| `imv questions` | Questions à poser maintenant |
| `imv pistes` | Aide à la résolution : hypothèses classées, vérifications à faire, contournements, pistes déjà écartées |
| `imv timeline` | Chronologie des événements |
| `imv actions` | Actions identifiées, owners, statuts |
| `imv com [direction\|utilisateurs\|tech]` | Brouillon de communication pour l'audience choisie |
| `imv ? <question>` | Question libre : réponse tirée du chat, complétée si utile par des pistes |
| `imv rapport` | Aperçu du résumé de fin, sans clore |
| `imv clore` | Clôture (responsable uniquement) : publie le résumé de fin, marque l'incident clos et quitte la réunion |

| `imv autoriser <nom>` / `imv autoriser tous` | Autorise une personne, ou tout le monde (responsable uniquement) |
| `imv retirer <nom>` / `imv retirer tous` | Retire une autorisation, ou réserve de nouveau les commandes (responsable uniquement) |
| `imv autorisés` | Responsable et personnes autorisées |

#### Fin de l'incident

Quand quelqu'un annonce dans le chat, ou à l'oral avec `--captions`, que l'incident est terminé (« c'est résolu », « le service est rétabli », « on peut clore »…), l'agent demande au responsable s'il faut clore. Le responsable répond « oui » dans les 10 minutes, ou tape `imv clore`. L'agent publie alors le résumé de fin (incident et durée, résumé, chronologie, cause, résolution, actions restantes, points pour le post-mortem), puis quitte la réunion. Il ne clôt jamais de lui-même.

- La proposition n'est faite qu'une fois par période de 10 minutes, jamais en pause, et pas pour une phrase négative (« ce n'est pas encore résolu »).
- Sans `--llm`, le résumé de fin se limite aux faits enregistrés : nombre de messages, heures, participants.

#### Qui peut utiliser les commandes

Les commandes sont réservées au **responsable d'IMV** et aux personnes qu'il autorise ; seule `imv aide` est ouverte à tous.

- Le responsable est désigné au lancement par `--im "Prénom Nom"` (répétable), ou par `IMV_IM` dans `.env`. Indiquez le nom **exactement comme Teams l'affiche** (la casse et les accents sont ignorés). Sans `--im`, la première personne qui tape une commande `imv` devient responsable, et l'agent l'annonce dans le chat.
- Dans la réunion, le responsable tape `imv autoriser Sophie` : un nom partiel est complété d'après les participants qui ont déjà écrit (« Sophie Lambert »), s'il n'y en a qu'un qui correspond. `imv autoriser tous` ouvre les commandes à tout le monde, `imv retirer tous` les réserve de nouveau.
- Une personne non autorisée reçoit une seule réponse, qui lui indique comment demander l'accès ; ses commandes suivantes sont ignorées sans réponse, pour ne pas encombrer le chat.
- Un invité choisit librement son nom : Teams le suffixe alors de « (Guest) » ou « (Unverified) ». Ce suffixe est pris en compte, si bien qu'un invité nommé comme le responsable n'a pas ses droits.
- La liste est tenue en mémoire pour la durée de la session : elle repart de zéro au prochain lancement.

Les commandes d'analyse demandent l'option `--llm` (voir ci-dessous). Si le chat est en lecture seule pour les invités, les réponses s'affichent seulement dans la console. Les commandes déjà présentes dans le chat à l'arrivée de l'agent ne sont pas exécutées.

### Analyse (LLM)

| `--llm` | Usage | Configuration | Modèle par défaut |
| --- | --- | --- | --- |
| `claude-cli` | Mac perso, prototype ; serveur Docker (`deploy/README.md`) | Claude Code installé et connecté (`claude`), ou jeton `CLAUDE_CODE_OAUTH_TOKEN` ; consomme l'abonnement | `opus` |
| `foundry` | Réseau de la société : Claude sur Microsoft Foundry | `AZURE_FOUNDRY_ENDPOINT` + Entra ID (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`) ou `ANTHROPIC_FOUNDRY_API_KEY` | `ANTHROPIC_FOUNDRY_MODEL`, sinon `claude-opus-5-5` |
| `azure-openai` | Réseau de la société : GPT sur Microsoft Foundry | `AZURE_FOUNDRY_ENDPOINT` (ou `AZURE_OPENAI_ENDPOINT`) + Entra ID ou `AZURE_OPENAI_API_KEY` | `OPENAI_MODEL` |
| `anthropic` | API Anthropic directe | `ANTHROPIC_API_KEY` | `claude-opus-5-5` |
| `ollama` | Modèle local, rien ne sort de la machine | Ollama lancé (`OLLAMA_HOST` si autre que `localhost:11434`) | `qwen3:14b` |

Les variables peuvent être dans l'environnement ou dans un fichier `.env` du répertoire courant (modèle : `.env.example` ; l'environnement est prioritaire). `python imo_agent.py check-llm --llm foundry` vérifie la connexion. `--llm-model` change le modèle. Les mots de passe et jetons repérés dans le chat sont masqués avant l'envoi au LLM. Avec `claude-cli`, le contenu du chat part vers votre compte Claude personnel : réservez-le à des réunions de test tant que le RSSI n'a pas validé.

IMV n'est pas qu'un secrétaire : il aide à résoudre avec son expertise (exploitation, réseau, bases de données, cloud…), qu'il présente toujours comme des « pistes », distinctes des faits tirés du chat. Pour des pistes adaptées à votre SI, décrivez-le dans un fichier de contexte (applications, architecture, équipes, escalades, incidents connus) : copiez `contexte.exemple.md` en `contexte.md` (non versionné), il est alors chargé automatiquement depuis le répertoire courant ; un autre fichier se choisit avec `--context` ou `IMV_CONTEXT` dans `.env`. Ce fichier part au LLM avec chaque analyse : aucun secret dedans.

```bash
python imo_agent.py join "<url>" --llm claude-cli      # charge contexte.md s'il existe
python imo_agent.py analyze 5 resume --llm ollama      # rejoue une analyse sur une session enregistrée
```

Sur le réseau d'entreprise, renseigner `HTTP_PROXY` et `HTTPS_PROXY` dans `.env` si
Entra ID nécessite un proxy. Les SDK Azure et OpenAI lisent ces variables standard ;
`PROXY_CONFIGURED` seul n'a aucun effet ici. Utiliser `NO_PROXY` pour les domaines
internes à joindre directement (exemple : `localhost,127.0.0.1,::1,.socgen`).
Une erreur `Failed to resolve 'login.microsoftonline.com'` indique un problème
DNS/réseau avant la vérification des identifiants, pas nécessairement un refus
d'authentification. Vérifier ensuite avec
`python imo_agent.py check-llm --llm azure-openai`.

### Serveur web local : plusieurs réunions à la fois

```bash
.venv/bin/python imo_agent.py serve --llm azure-openai    # puis ouvrir http://127.0.0.1:8765
```

La page permet de lancer un agent dans une réunion (lien, nom, responsables, incident, LLM, transcription de la voix, avatar), de suivre chaque réunion en direct (messages du chat, phrases dites à l'oral, réponses d'IMV, journal de l'agent), d'**écrire dans le chat de la réunion** au nom de l'agent, de faire quitter la réunion à un agent, et de **relancer** une session terminée (nouvelle session avec le même lien, le même nom et les mêmes options). L'**historique** des sessions (y compris celles lancées avec `join`) reste consultable après un redémarrage du serveur : il est lu dans la base. Une session restée ouverte lors d'un arrêt brutal apparaît comme « Interrompue » ; le journal de l'agent n'est disponible que pendant la session. Chaque agent a son propre navigateur, caché ; tous partagent la même base (`--db`) et le même `contexte.md`.

- Options : `--port` (défaut 8765), `--llm`/`--llm-model` (fournisseur proposé par défaut dans la page), `--context`, `--browser`, `--show`, `--admission-timeout`, `--debug-dir` (un sous-dossier `session-<n>` par réunion), `--auth`, `--host`, `--allowed-host`, `-v`.
- Tant qu'aucun utilisateur n'existe dans la base, le serveur n'écoute que `127.0.0.1` et n'a pas d'authentification : il est réservé à votre poste. **Dès qu'un utilisateur est créé, la connexion devient obligatoire** (au prochain démarrage du serveur). `--no-auth` la désactive malgré tout (poste local seulement) ; `--host` autre que `127.0.0.1` est refusé sans connexion.
- Les agents tournent dans le processus du serveur : **arrêter ou redémarrer le serveur termine leurs sessions**. Ctrl+C fait quitter proprement la réunion à chaque agent (session « Terminée », arrêtée par le serveur) ; il suffit ensuite de cliquer « Relancer ». Après un arrêt brutal (processus tué, double Ctrl+C), Teams peut afficher l'agent encore quelques minutes. Les sessions créées avant l'enregistrement des options proposent « Reprendre dans le formulaire » au lieu de « Relancer ».
- Comptez environ 300 à 500 Mo de mémoire par réunion.
- Tout ce que fait la page passe aussi par l'API JSON : voir « API » ci-dessous.

#### Utilisateurs et groupes

Dès qu'un utilisateur existe (ou avec `--auth`), il faut se connecter (identifiant et mot de passe) et les droits dépendent du **rôle** et des **groupes** :

| Rôle | Droits |
| --- | --- |
| `admin` (administrateur) | Gère les utilisateurs et les groupes ; voit et pilote toutes les réunions |
| `operator` (opérateur) | Lance, arrête, relance les agents et écrit dans le chat, pour les réunions de ses groupes |
| `viewer` (lecteur) | Consulte seulement les réunions de ses groupes |

Chaque réunion appartient à un groupe, choisi au lancement (le groupe de l'utilisateur s'il n'en a qu'un) ; une relance garde le groupe. Les réunions sans groupe (lancées avec `join`, d'avant les groupes, ou dont le groupe a été supprimé) ne sont visibles que des administrateurs.

```bash
python imo_agent.py user add erik --role admin           # mot de passe demandé (10 caractères minimum)
python imo_agent.py user add sophie --group SRE          # opératrice du groupe SRE (créé s'il n'existe pas)
python imo_agent.py user add paul --role viewer --group SRE --group DBA
python imo_agent.py user set paul --role operator        # --group remplace les groupes
python imo_agent.py user passwd sophie                   # nouveau mot de passe, connexions fermées
python imo_agent.py user list | user del <nom>
python imo_agent.py group add|del|list [<nom>]
python imo_agent.py serve --llm azure-openai             # connexion obligatoire ; refusé sans administrateur
```

Les administrateurs ont aussi, dans la page, un panneau « Administration » (utilisateurs, rôles, groupes, mots de passe) ; chacun peut changer son mot de passe (« Mon mot de passe ») et créer ses jetons d'API (« Mes jetons d'API »).

- Mots de passe hachés avec scrypt ; connexion valable 7 jours (cookie `HttpOnly`, `SameSite=Strict`, `Secure` en HTTPS) ; seule l'empreinte du jeton est en base. Changer un mot de passe ferme les autres connexions de l'utilisateur.
- 10 échecs de connexion en 15 minutes pour un même identifiant bloquent les essais suivants le temps que la fenêtre passe.
- Le dernier administrateur ne peut être ni supprimé, ni rétrogradé.
- En ligne : gardez `--host 127.0.0.1` derrière un proxy HTTPS (Caddy, nginx) qui transmet l'en-tête `Host` et `X-Forwarded-Proto`, et déclarez le nom de domaine avec `--allowed-host imv.exemple.com`.

#### API

Tout se fait aussi par l'API JSON : rejoindre une réunion, suivre la conversation, écrire dans le chat, lancer les commandes de l'agent, analyser une session, quitter, relancer, administrer. Documentation interactive (Swagger) sur **`/api/docs`**, après connexion ; schéma OpenAPI sur `/api/openapi.json`.

Avec la connexion obligatoire, un script s'authentifie avec un **jeton d'API**, qui a les droits de son utilisateur (rôle et groupes) : créé dans la page (« Mes jetons d'API ») ou en ligne de commande, il n'est affiché qu'une fois et seule son empreinte est en base. Pour un script, créez de préférence un utilisateur dédié (ex. `supervision`, rôle et groupes adaptés).

```bash
python imo_agent.py user add supervision --group SRE              # compte de service, opérateur
python imo_agent.py token add supervision --name "astreinte" --days 365   # affiche imv_…
python imo_agent.py token list | token del <id>
```

```bash
H="Authorization: Bearer imv_…"; IMV=https://imv.exemple.com
# Rejoindre une réunion (201 ; puis interroger l'état jusqu'à "in_meeting")
curl -s -H "$H" -H "Content-Type: application/json" -X POST $IMV/api/meetings \
     -d '{"url": "https://teams.microsoft.com/…", "incident": "INC42", "im": ["Sophie Lambert"], "group_id": 1}'
curl -s -H "$H" $IMV/api/meetings/12                          # état : joining, in_meeting, ended…
# Commande de l'agent, publiée dans le chat et renvoyée (comme « imv résumé » tapé par un responsable)
curl -s -H "$H" -H "Content-Type: application/json" -X POST $IMV/api/meetings/12/command -d '{"command": "résumé"}'
# Analyse sans rien publier dans la réunion (aussi pour une session terminée)
curl -s -H "$H" -H "Content-Type: application/json" -X POST $IMV/api/meetings/12/analyze -d '{"command": "timeline"}'
curl -s -H "$H" -X DELETE $IMV/api/meetings/12                # quitter la réunion (message d'au revoir)
```

| Route | Rôle minimal | Usage |
| --- | --- | --- |
| `GET /api/meetings` · `GET /api/meetings/<id>` | lecteur | Sessions visibles (historique compris) · état d'une session |
| `GET /api/meetings/<id>/feed?limit=300` | lecteur | État, messages (chat, oral, réponses de l'agent), journal de l'agent |
| `POST /api/meetings` | opérateur | Rejoindre : `url`, `name`, `im`, `incident`, `group_id`, `captions`, `captions_language` (`fr`/`en`), `avatar`, `welcome`, `llm` |
| `POST /api/meetings/<id>/command` | opérateur | `{"command": "résumé"}` : toute commande « imv … » (`pistes`, `com direction`, `? question`, `démarrer INC42`, `pause`, `autoriser Sophie`, `clore`…), réponse publiée et renvoyée (`text`, `delivered`) |
| `POST /api/meetings/<id>/analyze` | opérateur | `{"command": "rapport", "language": "en"}` : analyse LLM renvoyée sans publication, aussi après la réunion |
| `POST /api/meetings/<id>/chat` | opérateur | `{"text": "…"}` : message libre au nom de l'agent |
| `POST /api/meetings/<id>/restart` · `DELETE /api/meetings/<id>` | opérateur | Relancer une session terminée · quitter la réunion |
| `GET /api/metrics` | lecteur | Métriques Prometheus (format texte) : agents en cours, sessions, échecs, messages, appels au LLM |
| `GET /api/options` | lecteur | Fournisseurs LLM, groupes où l'utilisateur peut lancer un agent |
| `GET /api/me` · `POST /api/me/password` | lecteur | Utilisateur courant · changer son mot de passe (`{"current", "new"}`) |
| `GET\|POST /api/tokens` · `DELETE /api/tokens/<id>` | lecteur | Ses jetons (`{"name", "days"}`) ; administrateurs : `?all=true`, `"user_id"` pour un autre utilisateur. Création depuis une connexion par mot de passe seulement |
| `POST /api/login` · `POST /api/logout` | — | Connexion par cookie (`{"username", "password"}`), pour la page |
| `GET\|POST /api/users` · `PATCH\|DELETE /api/users/<id>` | admin | Utilisateurs (`{"username", "password", "role", "group_ids"}`) |
| `GET\|POST /api/groups` · `DELETE /api/groups/<id>` | admin | Groupes (`{"name"}`) |

Codes : 401 non connecté ou jeton invalide, 403 droits insuffisants, 404 session inconnue ou hors de vos groupes, 409 agent pas (ou plus) en réunion, 400 demande invalide, 502 LLM en erreur. Sans connexion (aucun utilisateur, ou `--no-auth`), les mêmes routes sont ouvertes sans jeton (poste local).

### Historique

```bash
.venv/bin/python imo_agent.py history      # liste des sessions
.venv/bin/python imo_agent.py history 3    # messages de la session 3
```

### Codes d'échec

| Code | Signification probable |
| --- | --- |
| `login_required` | Teams exige une connexion : les invités anonymes sont interdits pour cette réunion |
| `invalid_launcher` | La destination du lanceur n'est pas une URL web Teams valide, ou renvoie vers un autre lanceur |
| `prejoin_blocked` | « Join now » reste désactivé (nom refusé, ou politique du tenant) |
| `rejected` | Un participant a refusé l'admission |
| `admission_timeout` | Personne n'a admis l'agent à temps |
| `meeting_ended` | La réunion s'est terminée pendant l'attente |
| `chat_unavailable` | Bouton du chat introuvable (chat désactivé, ou sélecteurs à ajuster) |

Pendant la phase de prototype, une capture `debug/<date>-chat.{png,html}` est aussi faite en fin de session : elle **contient les messages du chat**, à supprimer après usage. En cas d'échec, une capture d'écran et le HTML de la page sont enregistrés dans `debug/` : ils servent à ajuster les sélecteurs dans `imo/connectors/teams/selectors.py` et `chat_reader.js`.
Après le clic automatique sur « Join now », si ni la réunion ni la salle d'attente ne sont
reconnues pendant 10 secondes, une capture `admission-pending.{png,html}` est enregistrée,
même sans fenêtre visible (`debug/session-<id>/` avec le serveur).

## Déploiement en ligne

Serveur en ligne avec Docker et Traefik (HTTPS Let's Encrypt) : voir [deploy/README.md](deploy/README.md).

## Tests

```bash
.venv/bin/pytest
```

Les tests rejouent le parcours (pré-connexion, salle d'attente, admission, chat, écriture, commandes, fin de réunion, refus, redirection vers la connexion) contre une **imitation locale de Teams** (`tests/fixtures/fake_teams.html`). Ils ne remplacent pas un essai sur une vraie réunion : le DOM réel de Teams change régulièrement.

## Limites connues

- Les sélecteurs dépendent de l'interface Teams web en anglais (le navigateur est forcé en `en-US`) et devront être ajustés après les premiers essais réels, y compris la zone de saisie du chat (`CHAT_INPUT`).
- L'agent reconnaît ses propres messages par son nom ou par leur texte : à vérifier sur le vrai Teams, qui peut les afficher sans auteur.
- Les messages postés par d'autres bots, les modifications et suppressions de messages ne sont pas encore gérés.
- L'analyse relit tout le chat à chaque commande (pas encore d'état structuré ni de résumé glissant) : suffisant pour des calls de quelques heures.
- Suggestions proactives limitées à deux cas : secret collé dans le chat, et fin d'incident évoquée (Étape 2b).
