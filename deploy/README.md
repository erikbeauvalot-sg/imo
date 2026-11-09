# Déploiement d'IMV sur un VPS (Docker + Traefik)

Deux conteneurs : **Traefik**, seul exposé sur Internet (ports 80 et 443 de l'hôte, certificat Let's Encrypt, redirection HTTP → HTTPS, en-têtes de sécurité), et **IMV**, sans port publié, joint par Traefik sur le réseau Docker. Traefik utilise le réseau de l'hôte : il voit l'adresse IP réelle des visiteurs et peut aussi publier des services de l'hôte (voir « Autres services de l'hôte »). La connexion par identifiant et mot de passe est obligatoire (voir « Utilisateurs et groupes » dans le README principal).

## Prérequis

- Un VPS Linux (Ubuntu 24.04 ou équivalent) avec Docker Engine et le plugin Compose.
- Un nom de domaine dont l'enregistrement DNS A (et AAAA s'il y a de l'IPv6) pointe vers le VPS.
- Ports 80 et 443 ouverts (Let's Encrypt valide le certificat par le port 443).
- Mémoire : environ 1 Go pour la base, plus 300 à 500 Mo par réunion suivie en même temps.

## Installation

```bash
git clone https://github.com/erikbeauvalot-sg/imo.git && cd imo/deploy
cp .env.example .env && nano .env            # domaine, e-mail Let's Encrypt, LLM et ses identifiants
install -d -o 1001 -g 1001 data              # données d'IMV, écrites par l'utilisateur du conteneur (uid 1001)
cp ../contexte.exemple.md data/contexte.md && chown 1001:1001 data/contexte.md   # facultatif : à adapter
docker compose build
docker compose run --rm imv user add erik --role admin   # premier administrateur (mot de passe demandé)
docker compose up -d
```

Puis ouvrir `https://<IMV_DOMAIN>`. Le certificat est obtenu au premier accès (quelques secondes). IMV refuse de démarrer tant qu'aucun administrateur n'existe.

## Exploitation

| Action | Commande (dans `deploy/`) |
| --- | --- |
| État, journaux | `docker compose ps` · `docker compose logs -f imv` · `docker compose logs -f traefik` |
| Utilisateurs, groupes, jetons | `docker compose exec imv python3 /app/imo_agent.py user list` (de même `user add`, `group add`, `token add`…) |
| Mise à jour | `git pull && docker compose up -d --build` |
| Redémarrage | `docker compose restart imv` |
| Sauvegarde de la base | `sqlite3 data/sessions/imv.db ".backup imv-$(date +%F).db"` (sur l'hôte, paquet `sqlite3`) |

- **Arrêter ou redémarrer IMV termine les réunions en cours** : chaque agent dit au revoir et quitte sa réunion (60 s accordées), puis « Relancer » dans la page.
- `data/` contient tout l'état : la base (`sessions/imv.db` : utilisateurs, réunions, **messages des chats**), les captures de diagnostic (`debug/`, qui contiennent aussi des messages : à purger) et `contexte.md`, relu à chaque lancement d'agent (inutile de redémarrer IMV après une modification).
- `letsencrypt/acme.json` contient la clé privée du certificat : ni copie, ni partage.

## Sécurité

- Pare-feu : n'ouvrir que SSH, 80 et 443 (`ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`). Traefik écoute directement sur l'hôte : ces règles s'appliquent. IMV ne publie aucun port.
- Traefik lit le socket Docker (en lecture seule) pour découvrir IMV : un accès à Traefik vaut un accès à Docker. Gardez son image à jour.
- Les identifiants du LLM sont dans `deploy/.env` (droits `600` conseillés : `chmod 600 .env`).
- Le serveur limite les essais de connexion, n'accepte que le nom de domaine déclaré et refuse les requêtes envoyées par d'autres sites ; le cookie de connexion est `Secure` (HTTPS) et `HttpOnly`.

## LLM : Claude avec votre abonnement (`claude-cli`)

L'image contient Claude Code (version figée dans le `Dockerfile`, sans mise à jour automatique). Comme sur un poste, `--llm claude-cli` passe par votre abonnement Claude ; dans le conteneur, la connexion se fait par un jeton longue durée :

1. Sur un poste où vous êtes connecté à Claude Code, dans un terminal : `claude setup-token` (ouvre le navigateur, puis affiche un jeton `sk-ant-oat…`, valable un an). Copiez-le ; ne le collez ni dans un chat, ni dans un dépôt.
2. Dans `deploy/.env` : `IMV_LLM=claude-cli` et `CLAUDE_CODE_OAUTH_TOKEN=<jeton>`, puis `docker compose up -d imv`.
3. Vérification : `docker compose exec imv python3 /app/imo_agent.py check-llm --llm claude-cli`.

Les analyses sont décomptées de votre abonnement, avec les mêmes limites d'usage que votre Claude Code ; le contenu des réunions part vers votre compte Claude personnel (voir la réserve RSSI du README principal). Pour un usage partagé ou en production, préférez une clé d'API (`IMV_LLM=anthropic` et `ANTHROPIC_API_KEY`), facturée à l'usage. Un jeton expiré ou révoqué donne « Not logged in » dans le journal de l'agent : recommencez l'étape 1.

## Autres services de l'hôte

Traefik peut publier en HTTPS un service qui tourne sur le VPS hors Docker, en écoute sur `127.0.0.1` (par exemple un tableau de bord d'administration) : copier `traefik/hote.yml.example` en `traefik/<nom>.yml`, adapter le nom de domaine (enregistrement DNS vers le VPS), le port et les adresses IP autorisées. Ces fichiers ne sont pas versionnés et Traefik les prend en compte sans redémarrage. Le service garde sa propre authentification ; le filtre IP (`ipAllowList`) la complète.

## Agent Hermes (`hermes`)

Si l'agent [Hermes](https://hermes-agent.nousresearch.com) tourne sur le même VPS, IMV peut s'en servir comme LLM (`IMV_LLM=hermes`) et, quel que soit le LLM des analyses, comme agent de recherche facultatif de la commande `imv recherche <sujet>` (recherche web, mémoire d'un incident à l'autre). Sans Hermes, Oscar fonctionne de la même façon avec n'importe quel LLM, sans parler de recherche web.

1. **Profil dédié** : `hermes profile create oscar --clone` (modèle et réglages du profil principal, sans bots de messagerie). Le contenu des réunions vient des participants : ne lui laisser que des outils sans effet sur le serveur, par exemple `hermes -p oscar config set platform_toolsets.api_server "[web, memory]"` et la liste des autres boîtes à outils dans `agent.disabled_toolsets` (terminal, fichiers, code, navigateur…). Remplacer son `SOUL.md` par une consigne propre à Oscar.
2. **API** : `API_SERVER_ENABLED=true` et une clé `API_SERVER_KEY` dans `~/.hermes/.env` (profil principal, écoute sur `127.0.0.1:8642`), une autre clé `API_SERVER_KEY` dans `~/.hermes/profiles/oscar/.env`, puis redémarrer le gateway. Avec le gateway multiplexé, le profil répond sous `/p/oscar/v1` et n'accepte que sa propre clé.
3. **Accès depuis le conteneur** : copier `traefik/hermes-api.yml.example` (seul le préfixe `/p/oscar/` est routé, par le point d'entrée privé `hote-imv`), puis `HERMES_API_KEY=<clé du profil oscar>` dans `deploy/.env` et `docker compose up -d imv`.
4. **Vérification** : `docker compose exec imv python3 /app/imo_agent.py check-llm --llm hermes`.

## Supervision (Prometheus)

Trois sources de métriques, pour un Prometheus distant :

| Source | Adresse | Accès |
| --- | --- | --- |
| IMV : agents en cours, sessions et échecs, messages, appels au LLM (`imv_*`) | `https://<IMV_DOMAIN>/api/metrics` | Jeton d'API d'un utilisateur dédié (rôle lecteur suffit) : `user add prometheus --role viewer`, puis `token add prometheus --name prometheus` |
| Hôte (node_exporter, paquet `prometheus-node-exporter` écoutant sur `127.0.0.1:9100`) | `https://<IMV_DOMAIN>/metrics/node` | Route Traefik `traefik/supervision.yml` (modèle `.example`) : filtre IP + mot de passe |
| Traefik : requêtes, codes et durées par route (IMV, autres services), certificats | `https://<IMV_DOMAIN>/metrics/traefik` | Idem |

Côté Prometheus : `scheme: https`, `metrics_path` ci-dessus, `authorization` (jeton, `credentials_file`) pour IMV et `basic_auth` (`password_file`) pour les deux autres. Une sonde blackbox sur `https://<IMV_DOMAIN>/login` complète l'ensemble (disponibilité, expiration du certificat).

## Limites

- Teams voit l'agent arriver depuis l'adresse IP du VPS (hébergeur) : l'accueil en salle d'attente reste nécessaire.
- La version de Playwright du `Dockerfile` (image et paquet Python) doit rester identique des deux côtés.

## Essai en local

Avec `IMV_DOMAIN=imv.localtest.me` (nom qui pointe vers 127.0.0.1), `HTTP_PORT=8080` et `HTTPS_PORT=8443` dans `.env` (Docker Desktop : activer *Settings → Resources → Network → Enable host networking*, Traefik étant sur le réseau de l'hôte) : `https://imv.localtest.me:8443`, avec le certificat par défaut de Traefik (Let's Encrypt ne peut pas valider un nom local), à accepter dans le navigateur.
