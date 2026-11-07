# IMV — Incident Manager Virtuel (prototype)

Prototype de l'Étape 0 (voir `PLAN.md` et `ETUDE_FAISABILITE.md`) : un agent qui **rejoint une réunion Teams par son URL, comme invité**, via un navigateur automatisé (Playwright), puis **lit le chat** en continu. Aucune API Graph ni app Teams n'est nécessaire.

## Installation

```bash
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
.venv/bin/playwright install chromium
```

## Utilisation

```bash
.venv/bin/python imo_agent.py join "<url de la réunion>"
```

1. Un navigateur Chromium s'ouvre et rejoint la réunion sous le nom « IMV - Assistant IM », sans caméra ni micro.
2. Un participant doit **l'admettre depuis la salle d'attente**.
3. L'agent ouvre le chat et y poste un **message d'accueil** qui annonce l'enregistrement (si le chat est ouvert aux invités).
4. Chaque message s'affiche dans la console et est enregistré dans la base SQLite `sessions/imv.db`.
5. `Ctrl+C` pour quitter la réunion.

Options : `--name` (nom affiché : lettres, chiffres, espaces et `- ' . _ @` uniquement), `--incident INC…`, `--no-welcome`, `--db`, `--out` (copie JSONL), `--headless`, `--admission-timeout`, `--debug-dir`, `-v`.

### Commandes dans le chat

Un invité ne peut pas être @mentionné : les commandes commencent par `imv`.

| Commande | Effet |
| --- | --- |
| `imv aide` | Liste des commandes |
| `imv démarrer [ID incident]` | Démarre le suivi de l'incident ; les messages déjà captés y sont rattachés |
| `imv statut` | Incident suivi, état de l'enregistrement, nombre de messages |
| `imv pause` / `imv reprise` | Suspend / reprend l'enregistrement du chat |

Les commandes d'analyse (`résumé`, `timeline`, `actions`…) sont reconnues mais pas encore disponibles (Étape 2). Si le chat est en lecture seule pour les invités, les réponses s'affichent seulement dans la console. Les commandes déjà présentes dans le chat à l'arrivée de l'agent ne sont pas exécutées.

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
- Pas encore d'analyse (résumés, suggestions) : c'est l'Étape 2 du plan.
