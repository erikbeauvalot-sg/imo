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

## Utilisation

```bash
.venv/bin/python imo_agent.py join "<url de la réunion>"
```

1. Un navigateur Chromium s'ouvre et rejoint la réunion sous un nom tiré au hasard, « IMV - » suivi d'un nom d'étoile (« IMV - Nova », « IMV - Orion »…), sans caméra ni micro. Plusieurs agents dans une même réunion restent ainsi distincts. `--name` ou `IMV_NAME` dans `.env` impose un nom.
2. Un participant doit **l'admettre depuis la salle d'attente**.
3. L'agent ouvre le chat et y poste un **message d'accueil** qui annonce l'enregistrement (si le chat est ouvert aux invités).
4. Chaque message s'affiche dans la console et est enregistré dans la base SQLite `sessions/imv.db`.
5. `Ctrl+C` pour quitter la réunion.

Options : `--context FICHIER` (défaut : `contexte.md` du répertoire courant s'il existe), `--im NOM` (responsable d'IMV, voir « Qui peut utiliser les commandes »), `--name` (nom affiché : lettres, chiffres, espaces et `- ' . _ @` uniquement), `--incident INC…`, `--no-welcome`, `--avatar [IMAGE]` (caméra allumée avec le robot IMV, ou votre image PNG/JPEG : un invité n'a pas de photo de profil), `--db`, `--out` (copie JSONL), `--headless`, `--admission-timeout`, `--debug-dir`, `-v`.

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

| `imv autoriser <nom>` / `imv autoriser tous` | Autorise une personne, ou tout le monde (responsable uniquement) |
| `imv retirer <nom>` / `imv retirer tous` | Retire une autorisation, ou réserve de nouveau les commandes (responsable uniquement) |
| `imv autorisés` | Responsable et personnes autorisées |

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
| `claude-cli` | Mac perso, prototype | Claude Code installé et connecté (`claude`) ; consomme l'abonnement | `opus` |
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

### Historique

```bash
.venv/bin/python imo_agent.py history      # liste des sessions
.venv/bin/python imo_agent.py history 3    # messages de la session 3
```

### Codes d'échec

| Code | Signification probable |
| --- | --- |
| `login_required` | Teams exige une connexion : les invités anonymes sont interdits pour cette réunion |
| `prejoin_blocked` | « Join now » reste désactivé (nom refusé, ou politique du tenant) |
| `rejected` | Un participant a refusé l'admission |
| `admission_timeout` | Personne n'a admis l'agent à temps |
| `meeting_ended` | La réunion s'est terminée pendant l'attente |
| `chat_unavailable` | Bouton du chat introuvable (chat désactivé, ou sélecteurs à ajuster) |

Pendant la phase de prototype, une capture `debug/<date>-chat.{png,html}` est aussi faite en fin de session : elle **contient les messages du chat**, à supprimer après usage. En cas d'échec, une capture d'écran et le HTML de la page sont enregistrés dans `debug/` : ils servent à ajuster les sélecteurs dans `imo/connectors/teams/selectors.py` et `chat_reader.js`.

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
- Pas encore de suggestions proactives ni de commandes `com` et `clore` (Étape 2b/2c).
