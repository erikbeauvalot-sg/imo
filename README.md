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

Si `playwright install chromium` échoue (proxy ou filtrage du réseau d'entreprise), inutile d'insister : l'agent utilise alors **Google Chrome ou Microsoft Edge déjà installés** (essayés dans cet ordre si le Chromium de Playwright est absent). Pour imposer un navigateur : `--browser chrome|msedge|chromium`, ou `IMV_BROWSER=msedge` dans `.env`. Dans tous les cas un profil temporaire vierge est utilisé, jamais votre profil personnel. Pour retenter le téléchargement derrière un proxy : `HTTPS_PROXY=http://proxy:port python -m playwright install chromium`.

`pyproject.toml` fait référence pour les dépendances ; `requirements.txt` et `requirements-dev.txt` doivent rester identiques, ce que vérifie `tests/test_requirements.py`. Après l'ajout d'une dépendance, mettre à jour les deux.

## Utilisation

```bash
.venv/bin/python imo_agent.py join "<url de la réunion>"
```

1. Un navigateur Chromium s'ouvre et rejoint la réunion sous le nom « IMV - Assistant IM », sans caméra ni micro.
2. Un participant doit **l'admettre depuis la salle d'attente**.
3. L'agent ouvre le chat et y poste un **message d'accueil** qui annonce l'enregistrement (si le chat est ouvert aux invités).
4. Chaque message s'affiche dans la console et est enregistré dans la base SQLite `sessions/imv.db`.
5. `Ctrl+C` pour quitter la réunion.

Options : `--name` (nom affiché : lettres, chiffres, espaces et `- ' . _ @` uniquement), `--incident INC…`, `--no-welcome`, `--avatar [IMAGE]` (caméra allumée avec le robot IMV, ou votre image PNG/JPEG : un invité n'a pas de photo de profil), `--db`, `--out` (copie JSONL), `--headless`, `--admission-timeout`, `--debug-dir`, `-v`.

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
| `imv timeline` | Chronologie des événements |
| `imv actions` | Actions identifiées, owners, statuts |

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

```bash
python imo_agent.py join "<url>" --llm claude-cli
python imo_agent.py analyze 5 resume --llm ollama      # rejoue une analyse sur une session enregistrée
```

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
