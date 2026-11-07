# Installer manuellement le Chromium de Playwright (macOS)

À utiliser quand `python -m playwright install chromium` échoue, par exemple derrière le proxy ou le filtrage d'un réseau d'entreprise. On télécharge les archives avec un navigateur, puis on les décompresse là où Playwright les cherche.

> Alternative sans téléchargement : utiliser Google Chrome ou Microsoft Edge déjà installés, avec `--browser chrome` ou `--browser msedge` (voir le README).

## 1. Obtenir les adresses exactes

La version de Chromium dépend de la version de Playwright installée par `pip`. Les adresses varient donc d'un poste à l'autre : lancez cette commande sur le poste à équiper. Elle ne télécharge rien.

```bash
.venv/bin/python -m playwright install --dry-run chromium
```

Pour chaque composant, elle affiche un `Install location` (le dossier de destination) et un `Download url` (l'archive à télécharger).

Exemple avec Playwright 1.64.1 sur un Mac Apple Silicon :

| Composant | Archive | Dossier de destination |
|---|---|---|
| Chromium | `https://cdn.playwright.dev/builds/cft/156.0.8078.4/mac-arm64/chrome-mac-arm64.zip` | `~/Library/Caches/ms-playwright/chromium-1248` |

- FFmpeg n'est pas nécessaire : il ne sert qu'à enregistrer des vidéos.
- Sur un Mac Intel, les adresses contiennent `mac-x64` au lieu de `mac-arm64`.

## 2. Télécharger et installer

Téléchargez les archives avec le navigateur, puis installez-les. Adaptez les numéros (`1248`) et les noms de fichiers à ce qu'affiche la commande de l'étape 1 :

```bash
D=~/Library/Caches/ms-playwright/chromium-1248
mkdir -p "$D"
unzip -q ~/Downloads/chrome-mac-arm64.zip -d "$D"
xattr -dr com.apple.quarantine "$D"
touch "$D/INSTALLATION_COMPLETE" "$D/DEPENDENCIES_VALIDATED"
```

Le « headless shell » listé par la commande n'est pas nécessaire : IMV cache la fenêtre avec le Chromium complet.

Résultat attendu :

```
~/Library/Caches/ms-playwright/chromium-1248/
├── chrome-mac-arm64/
├── DEPENDENCIES_VALIDATED
└── INSTALLATION_COMPLETE
```

## Points d'attention

- **`xattr` est indispensable.** macOS met en quarantaine les fichiers téléchargés par un navigateur, et bloquerait le lancement de « Google Chrome for Testing ».
- **Décompressez avec `unzip` dans le terminal**, plutôt qu'avec un double-clic dans le Finder : vous obtenez la bonne arborescence au bon endroit.
- **Les deux fichiers témoins sont nécessaires.** Sans `INSTALLATION_COMPLETE`, Playwright considère l'installation comme incomplète.
- **Le téléchargement peut aussi être bloqué dans le navigateur.** Les archives sont sur le même serveur que celui qu'utilise `playwright install`. Si le filtrage les refuse, utilisez `--browser msedge` ou `--browser chrome`.
- **Après une mise à jour de Playwright** (`pip install -U …`), le numéro de version de Chromium change : refaites ces étapes avec les nouvelles adresses.

## 3. Vérifier

Lancez l'agent sans l'option `--browser` :

```bash
.venv/bin/python imo_agent.py join "<url>" --llm azure-openai
```

Le terminal ne doit plus annoncer de repli sur Chrome ou Edge.
