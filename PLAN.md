# Plan de développement — Incident Manager Virtuel (IMV) pour Microsoft Teams

> Document de référence pour le développement. Basé sur `SPEC.md`.
> Version 0.4 — 2026-11-07 — choix de l'approche « participant navigateur » (Option 2)

---

## 1. Contexte et objectifs

Les Incident Managers (IM) ouvrent des calls techniques dans Teams pour résoudre les incidents. L'objectif est d'ajouter à ces calls un **Incident Manager Virtuel (IMV)** : un agent qui assiste l'IM humain sans le remplacer.

**Approche retenue** : l'agent **rejoint la réunion par son URL comme un participant invité**, via un navigateur automatisé, et lit le chat à l'écran. Elle ne nécessite **ni API Microsoft Graph ni application Teams** (non accessibles dans la société). Détail du choix : `ETUDE_FAISABILITE.md` §7.

### Objectifs mesurables

| # | Objectif | Indicateur |
| --- | --- | --- |
| O1 | Réduire le temps passé par l'IM à rédiger les résumés / communications | Temps de rédaction du point de situation < 1 min (vs. ~10 min) |
| O2 | Ne pas oublier les questions clés du processus incident | % de calls où impact, périmètre, owner, prochaine étape et prochain point sont renseignés |
| O3 | Faciliter l'arrivée d'un nouveau participant en cours de call | Résumé « catch-up » disponible à la demande en < 15 s |
| O4 | Produire un compte rendu exploitable en fin de call (post-mortem, ticket) | Rapport généré automatiquement à la clôture |

### Phasage

| Version | Étape | Contenu |
| --- | --- | --- |
| **Préalable** | **Étape 0 — Étude de faisabilité et état de l'art** | PoC technique, vérification des prérequis tenant, revue des solutions existantes (Microsoft, marché, open source), décision Go / No-go. Voir `ETUDE_FAISABILITE.md` |
| **V1** | **Étape 1 — Connexion au chat Teams** | L'agent rejoint le call par son URL (`imo_agent.py join <url>`), se présente et capte tous les messages écrits du chat |
| **V1** | **Étape 2 — Lecture et analyse** | L'agent analyse les messages pour produire résumés, suggestions et questions, puis le rapport de fin de call |
| **V2** | **Partie vocale** | Le même participant écoute l'audio du call et le transcrit — traité après la V1 |

### Hors périmètre de la V1

- Toute exploitation de l'audio / du transcript (→ V2).
- Aucune action technique (pas de redémarrage, pas d'accès prod).
- Pas de modification du ticket ITSM sans validation humaine.
- L'agent ne parle pas dans le call.

---

## 2. Utilisateurs et cas d'usage

### Personas

- **IM (Incident Manager)** : pilote le call, utilisateur principal.
- **Contributeurs techniques** : experts (réseau, DBA, applicatif…) qui écrivent dans le chat du call.
- **Manager IM / Management** : consomment les résumés et rapports.

### User stories

| ID | En tant que… | je veux… | afin de… | Étape |
| --- | --- | --- | --- | --- |
| US1 | IM | envoyer l'IMV dans mon call en donnant simplement son URL | ne pas perdre de temps au démarrage | V1 – Étape 1 |
| US2 | participant | que l'IMV annonce sa présence et ce qu'il capte | être informé (transparence / RGPD) | V1 – Étape 1 |
| US3 | IM | que tous les messages du chat soient captés, même sans mentionner l'IMV | ne rien perdre | V1 – Étape 1 |
| US4 | IM | demander un **résumé** de la situation à tout moment | faire un point de situation | V1 – Étape 2 |
| US5 | nouveau participant | obtenir un **catch-up** de ce qui s'est passé | me mettre à niveau sans interrompre | V1 – Étape 2 |
| US6 | IM | que l'IMV me **suggère les questions** à poser (impact, périmètre, changements récents, workaround…) | ne rien oublier | V1 – Étape 2 |
| US7 | IM | voir une **timeline** et la liste des **actions** (qui / quoi / quand) | suivre l'avancement | V1 – Étape 2 |
| US8 | IM | être **relancé proactivement** (pas d'owner, pas d'heure de prochain point, silence prolongé) | garder le rythme | V1 – Étape 2 |
| US9 | IM | obtenir un **brouillon de communication** et un **rapport de fin de call** | communiquer vite et alimenter le post-mortem | V1 – Étape 2 |
| US10 | IM | que tout cela intègre aussi **ce qui a été dit à l'oral** | avoir une vision complète | V2 |

---

## 3. Fonctionnalités détaillées

### 3.1 Étape 1 — Connexion au chat Teams

- **Lancement** : `imo_agent.py join <url>` (puis, plus tard, depuis l'interface web locale en collant l'URL).
- **Connexion** : l'agent ouvre l'URL dans Chromium, choisit « continuer dans le navigateur », saisit son nom (« IMV - Assistant IM »), coupe caméra et micro, puis clique « Join now ».
- **Salle d'attente** : un participant **admet** l'agent. L'agent détecte l'admission, le refus, la fin de réunion et les redirections vers la connexion Microsoft (invités interdits), avec un code d'erreur explicite.
- **Message d'accueil** à l'arrivée (si le chat est ouvert aux invités) : présentation de l'agent, information sur la captation du chat, rappel des commandes.
- **Capture de tous les messages** du chat : auteur, texte, horodatage, identifiant Teams ; sans doublon malgré le re-rendu de la liste.
- **Persistance** de chaque message (JSONL au prototype, puis base) rattaché à l'incident.
- **Diagnostic** : capture d'écran + HTML à chaque échec, pour ajuster les sélecteurs.
- **Commandes** tapées dans le chat avec un préfixe (`imv aide`, `imv démarrer INC0012345`, `imv pause` / `imv reprise`) : un invité ne peut pas être @mentionné comme un bot.

### 3.2 Étape 2 — Lecture et analyse

#### Commandes à la demande

Déclenchées dans le chat avec le préfixe `imv` (ex. `imv résumé`) ou depuis l'interface web locale de l'IM. Réponses en **texte mis en forme** dans le chat (si l'agent peut y écrire) et dans l'**interface web**, qui permet aussi de garder les suggestions privées pour l'IM.

| Commande | Description | Sortie |
| --- | --- | --- |
| `résumé` | Point de situation : symptômes, impact, hypothèses, actions en cours, prochaine étape | Message structuré |
| `catchup` | Résumé court pour un nouvel arrivant (depuis son arrivée si connue) | Message court |
| `questions` | Questions pertinentes non encore traitées, priorisées | Liste (« marquer traité » dans l'interface web) |
| `timeline` | Événements clés horodatés | Tableau chronologique |
| `actions` | Actions identifiées (owner, échéance, statut) | Tableau |
| `statut` | Checklist des éléments clés renseignés / manquants | Checklist |
| `com [audience]` | Brouillon de communication (management, utilisateurs, technique) | Texte prêt à copier |
| `clore` | Clôture : génère le rapport final | Rapport + export |

#### Suggestions proactives

L'agent analyse le flux de messages et intervient **avec parcimonie** :

| Déclencheur | Intervention |
| --- | --- |
| 10 min après `démarrer` et impact métier non identifié | « L'impact métier n'est pas encore clair : combien d'utilisateurs / quels services ? » |
| Aucun owner identifié sur une action | Suggestion d'assigner un owner |
| Aucune heure de prochain point de communication | Rappel |
| Silence > N min (configurable) dans le chat | Proposition de point de situation |
| Hypothèse mentionnée sans vérification après X min | « L'hypothèse "X" a-t-elle été validée ? » |
| Toutes les 30 min (configurable) | Mini-résumé automatique |

Règles anti-bruit (obligatoires) :

- Maximum 1 intervention proactive toutes les 5 min (configurable).
- Jamais deux fois la même suggestion si elle a été ignorée ou traitée.
- Mode `pause` respecté.
- Interventions courtes (≤ 3 lignes) et actionnables.

#### Rapport de fin de call

Contenu : identifiant incident, participants, durée, résumé exécutif, timeline, cause supposée/identifiée, actions réalisées, actions restantes (owner/échéance), points pour le post-mortem.
Formats : synthèse dans le chat + rapport complet Markdown/Word dans l'interface web + (option) note de travail poussée dans l'outil ITSM.

### 3.3 V2 — Partie vocale

- Fusion des sources **chat + transcript** dans une timeline unique, chaque élément marqué de sa source (💬 chat / 🎙️ oral).
- Le participant navigateur capte l'audio de la réunion et le transcrit (speech-to-text) ; toutes les commandes de la V1 en tiennent compte.
- Détection à l'oral des décisions, actions et hypothèses (« je m'en occupe », « on redémarre le nœud 2 »…).
- Voir §4.4 pour les options techniques.

---

## 4. Architecture

### 4.1 Vue d'ensemble

```text
          Réunion Teams (chat + audio)
                    ▲  rejoint par l'URL, comme invité
                    │  (admis depuis la salle d'attente)
┌───────────────────┼─────────── Serveur IMV (hébergé en local) ─────────────────┐
│                   │                                                             │
│   ┌───────────────┴──────────────┐      ┌─────────────────────────────────┐    │
│   │ Connecteur Teams navigateur  │      │ Interface                        │    │
│   │ (Playwright + Chromium)      │      │ - CLI : imo_agent.py join <url>  │    │
│   │ - connexion / salle d'attente│      │ - Web locale (FastAPI) : URL,    │    │
│   │ - lecture du chat   (Étape 1)│      │   résumés, suggestions privées   │    │
│   │ - écriture du chat  (Étape 2)│      └───────────────┬─────────────────┘    │
│   │ - capture audio     (V2)     │                      │                      │
│   └───────────────┬──────────────┘                      │                      │
│                   ▼                                     ▼                      │
│   ┌──────────────────────────────────────────────────────────────────┐        │
│   │ Moteur IMV : état de l'incident, règles proactives, rapport       │        │
│   │                        (Étape 2)        │                         │        │
│   └──────────────┬──────────────────────────┼─────────────────────────┘        │
│                  ▼                          ▼                                  │
│   ┌──────────────────────┐      ┌──────────────────────┐                       │
│   │ Base (SQLite →       │      │ LLM (Claude)         │                       │
│   │ PostgreSQL)          │      │ + STT en V2          │                       │
│   └──────────────────────┘      └──────────────────────┘                       │
└─────────────────────────────────────────────────────────────────────────────────┘
```

Une session = un navigateur = une réunion. L'application web lance une tâche de fond par réunion, ce qui permet plusieurs IM et plusieurs calls en parallèle.

### 4.2 Choix techniques

| Domaine | Choix | Justification / alternatives |
| --- | --- | --- |
| Langage | **Python 3.12** | Playwright, LLM et speech-to-text bien outillés |
| Connexion Teams | **Playwright + Chromium** (participant invité) | Aucune autorisation Graph ni app Teams. Sélecteurs repris de Vexa (Apache 2.0) |
| Interface | **CLI** (prototype), puis **FastAPI** + interface web simple | Usage local, puis « SaaS » interne |
| Hébergement | **Docker Compose** sur un serveur interne (Linux, Chromium headless ou Xvfb) | Hébergé en local, pas de cloud imposé |
| Persistance | **JSONL** (prototype) → **SQLite** → **PostgreSQL** | Montée en charge progressive |
| LLM | **Claude** (Sonnet 5.5 pendant le call, Opus 5.5 pour le rapport final), API Anthropic ou Microsoft Foundry | À arbitrer avec la sécurité |
| Speech-to-text (V2) | **faster-whisper** en local, ou service cloud | Local = aucune donnée audio ne sort |
| Observabilité | Logs structurés + OpenTelemetry | Latence, coûts LLM, échecs de connexion |
| CI | GitHub Actions ou Azure DevOps | Tests contre l'imitation locale de Teams |

### 4.3 Connexion et captation du chat (Étape 1)

- **Prototype réalisé** : `imo/connectors/teams/` (voir `README.md`) — parcours de connexion complet, lecture du chat, détection de fin de réunion, codes d'erreur, captures de diagnostic. Testé contre une imitation locale de Teams ; **reste à tester sur une vraie réunion**.
- Le navigateur est forcé en anglais (`en-US`) pour stabiliser les libellés ; tous les sélecteurs sont regroupés dans `selectors.py` et `chat_reader.js`.
- Le lecteur de chat observe le DOM (MutationObserver) : la liste est virtualisée, d'où une déduplication par identifiant de message.
- **Conditions côté réunion** (réglages Teams du tenant organisateur) : invités anonymes autorisés, admission depuis la salle d'attente, chat ouvert aux invités pour pouvoir écrire. Si l'agent est seulement autorisé à lire, il répond via l'interface web.
- **Écriture dans le chat** (Étape 2) : saisie dans la zone de message de la réunion, sous le nom de l'agent.

### 4.4 Captation de la voix (V2 — pour information)

Avec l'approche navigateur, l'audio arrive déjà dans Chromium : la piste naturelle est **C** (capture audio de la page + speech-to-text), comme le fait Vexa. A et B restent possibles si les API Microsoft deviennent accessibles.

| Approche | Principe | Avantages | Limites |
| --- | --- | --- | --- |
| **A. Graph Transcripts API** | Transcription Teams activée ; récupération via `GET /onlineMeetings/{id}/transcripts` (+ notifications `getAllTranscripts`) | Simple, natif | Transcript disponible **après** la réunion ; accès désactivé par défaut au niveau tenant |
| **B. Work IQ — transcript temps réel** | L'agent reçoit le transcript pendant la réunion, avec les noms des locuteurs | **Temps réel** sans infra média | GA prévue novembre 2026 ; réglage tenant, transcription lancée, app agissant au nom d'un utilisateur ; licences à vérifier |
| **C. Participant navigateur (notre agent, comme Vexa)** | Le navigateur capte l'audio de la réunion, transcrit en continu | Temps réel, aucune autorisation Microsoft | Speech-to-text à héberger, attribution des locuteurs imparfaite, validation sécurité |

Recommandation : **C**. Le spike V2 portera sur la capture audio (WebRTC dans la page) et la qualité de transcription en français.

### 4.5 Gestion du contexte LLM (Étape 2)

- **État structuré de l'incident** maintenu en base (JSON) : symptômes, impact, périmètre, hypothèses (statut), actions (owner/échéance/statut), décisions, timeline, questions traitées/ouvertes, prochain point.
- **Mise à jour incrémentale** : à chaque lot de nouveaux messages (debounce ~30 s ou N messages), un appel LLM « extracteur » met à jour l'état structuré (sortie JSON validée par schéma).
- Les commandes s'appuient sur l'état structuré + les derniers messages bruts → réponses rapides et peu coûteuses.
- **Résumé glissant** pour les calls longs (compression des messages anciens au-delà d'un seuil de tokens).
- **Prompt caching** sur le prompt système et l'historique stable.
- Rapport final : appel dédié avec l'historique complet (modèle le plus capable).

### 4.6 Modèle de données (initial)

```text
Incident        { id, itsmRef?, title, severity?, status, startedAt, closedAt?,
                  meetingUrl, imName, state (JSON), settings }
Message         { id, incidentId, source: chat|transcript, teamsMessageId?, authorName,
                  text, sentAt?, capturedAt }
Session         { id, incidentId, status: lobby|in_meeting|ended|failed, errorCode?,
                  joinedAt?, endedAt? }
BotIntervention { id, incidentId, type: command|proactive, ruleId?, content,
                  timestamp, feedback?: up|down|ignored }
Report          { incidentId, version, content (md/json), generatedAt }
```

Le champ `source` est prévu dès l'Étape 1 pour accueillir le transcript en V2 sans migration. Le prototype écrit déjà ces champs (`imo/models.py`).

---

## 5. Conception des prompts (Étape 2)

- **Prompt système « Incident Manager »** : rôle d'assistant d'un IM expérimenté, process ITIL, ton factuel et concis, en français, ne jamais inventer (citer la source / l'auteur), signaler l'incertitude.
- **Prompts spécialisés** : extracteur d'état (JSON), résumé, catch-up, questions, timeline, actions, communication (par audience), rapport final.
- **Référentiel de questions** (configurable, versionné) : impact métier, nb d'utilisateurs, périmètre géographique, début des symptômes, changement récent (CAB/déploiement), monitoring/alertes, workaround, escalade fournisseur, prochaine communication, critères de résolution…
- **Sorties structurées** validées par schéma (Pydantic) avant affichage ; retry sur échec de validation.
- **Garde-fous** : contenu du chat traité comme des données (protection contre l'injection de prompt), masquage des secrets/mots de passe collés dans le chat avant envoi au LLM.

---

## 6. Sécurité, conformité, RGPD

- Validation **DPO / RSSI** avant pilote : finalité, base légale, information des participants (message d'accueil de l'agent annonçant la captation).
- **Données** : chiffrement au repos et en transit ; hébergement UE ; **rétention** configurable (ex. 90 jours) puis purge automatique ; aucune donnée utilisée pour l'entraînement de modèles (vérifier les conditions du fournisseur LLM).
- **Accès** : interface web réservée aux IM (authentification) ; rapports accessibles à l'IM et aux managers IM.
- **Transparence** : l'agent est visible dans la liste des participants sous un nom explicite et annonce la captation à son arrivée ; il n'entre que s'il est admis.
- **Masquage** des données sensibles (mots de passe, tokens, IP si requis) avant appel LLM.
- **Journalisation** des appels LLM et traçabilité des interventions de l'agent.
- **Conditions d'utilisation** : l'automatisation du client Teams web n'est pas un usage prévu par Microsoft ; faire valider ce point (juridique / RSSI).
- **Données locales** : les fichiers `sessions/` contiennent le contenu des chats ; ils restent sur le serveur interne, avec rétention et purge.
- V2 : nouvelle validation DPO pour la captation de la voix / transcription.

---

## 7. Plan de réalisation

Hypothèse : 1 à 2 développeurs, sprints de 2 semaines. Estimations indicatives.

### Étape 0 : Étude de faisabilité et état de l'art (≈ 2 semaines)

Objectif : confirmer que le projet est faisable et utile **avant** de développer. Première passe documentaire réalisée : voir `ETUDE_FAISABILITE.md`.

| # | Tâche | Critère d'acceptation |
| --- | --- | --- |
| E0.1 | Recenser les usages des IM (type de call, durée, participants, bots déjà présents) | Note d'usages |
| E0.2 | Vérifier les réglages Teams des réunions IM : invités anonymes autorisés, salle d'attente, chat ouvert aux invités | Test manuel en navigation privée |
| E0.3 | PoC `imo_agent.py join <url>` (**prototype écrit**, voir `README.md`) sur de vraies réunions ; ajuster les sélecteurs | Admission et capture de 100 % des messages, dans chaque type de call |
| E0.4 | Tester Microsoft Facilitator sur 2–3 calls d'incident | Grille de couverture du besoin |
| E0.5 | Recenser les outils déjà en place (ITSM, PagerDuty, incident.io…) et leurs fonctions IA | Liste |
| E0.6 | Premier échange RSSI / DPO | Avis préliminaire |
| E0.7 | Estimation des coûts (serveur + LLM) | Estimation mensuelle |

**Jalon : décision Go / No-go** (critères dans `ETUDE_FAISABILITE.md` §6). En cas de Go, le prototype E0.3 sert de base à l'Étape 1.

### V1 — Étape 1 : Connexion au chat Teams (≈ 3 semaines)

Objectif : un agent qui rejoint un call Teams par son URL, se présente, et capte et stocke **tous** les messages du chat de façon fiable. Aucune analyse IA à ce stade.

| # | Tâche | Critère d'acceptation |
| --- | --- | --- |
| 1.1 | Repartir du prototype E0.3 et des types de call retenus en Étape 0 | Périmètre de l'Étape 1 figé |
| 1.2 | Fiabiliser la connexion : relances, URL courtes `teams.microsoft.com/meet/...`, code secret de réunion, fenêtres imprévues | 10 connexions réussies sur 10 réunions réelles |
| 1.3 | Fiabiliser la lecture du chat : sélecteurs ajustés, messages longs / multi-lignes, pièces jointes, réponses, modifications | Aucun message manqué sur les calls de test |
| 1.4 | Écriture dans le chat : message d'accueil (annonce de la captation) — **codé** (`send_chat`, `imo/agent.py`), à tester sur vrai Teams | Message visible par les participants (si le chat est ouvert aux invités) |
| 1.5 | Commandes `imv aide`, `imv démarrer [ID incident]`, `imv statut`, `imv pause` / `imv reprise` — **codé**, à tester sur vrai Teams | Commandes reconnues ; pause effective |
| 1.6 | Persistance SQLite : incidents, sessions, messages, interventions — **codé** (`imo/storage/`, `imo_agent.py history`) | 100 % des messages stockés avec auteur et horodatage |
| 1.7 | Mode serveur : Docker (Chromium headless / Xvfb), plusieurs réunions en parallèle | 3 réunions simultanées sans perte |
| 1.8 | Interface web locale minimale (FastAPI) : coller l'URL, voir l'état de la session et le chat capté | Démo à l'équipe IM |
| 1.9 | Surveillance de l'interface Teams : test quotidien sur une réunion de référence, alerte en cas de casse des sélecteurs | Alerte reçue lors d'une casse simulée |
| 1.10 | Test sur de vrais calls avec 1–2 IM | L'agent rejoint et capte le chat sans incident |

**Jalon : agent connecté au chat Teams.**

### V1 — Étape 2 : Lecture et analyse (≈ 8 semaines)

Objectif : l'agent exploite les messages captés pour proposer résumés, suggestions et rapport.

#### 2a — Analyse à la demande (≈ 3 semaines)

| # | Tâche | Critère d'acceptation |
| --- | --- | --- |
| 2.1 | Choix du fournisseur LLM + validation sécurité/DPO | Décision écrite |
| 2.2 | Ateliers IM : questions types, format attendu des résumés/rapports | Référentiel de questions v1 + gabarits validés |
| 2.3 | Client LLM (retries, timeouts, métriques coût/latence, prompt caching, masquage des secrets) — **codé** : fournisseurs `claude-cli`, `foundry`, `anthropic`, `ollama` (`imo/llm/`) | Tests unitaires + métriques visibles |
| 2.4 | Extracteur d'état incrémental (JSON validé par schéma) | État cohérent sur les scénarios de test |
| 2.5 | Commandes `résumé`, `catchup`, `timeline`, `actions`, `statut` (chat + interface web) — **codé** côté chat, sur le chat complet (`imo/analysis.py`) | Réponse < 15 s ; qualité ≥ 4/5 selon les IM |
| 2.6 | Commande `questions` basée sur le référentiel — **codé** avec une liste de thèmes dans le prompt | Questions pertinentes et non redondantes |
| 2.7 | Feedback 👍/👎 sur chaque réponse (interface web) | Feedback stocké |
| 2.8 | Jeu de scénarios « incidents dorés » + harnais d'évaluation (cf. §8) | Évaluation exécutée en CI |

**Jalon : pilote « à la demande » avec les IM.**

#### 2b — Suggestions proactives et communication (≈ 3 semaines)

| # | Tâche | Critère d'acceptation |
| --- | --- | --- |
| 2.9 | Moteur de règles proactives (déclencheurs §3.2, configurables) | Règles testées unitairement |
| 2.10 | Anti-bruit (rate limit, déduplication, respect de `pause`) | ≤ 1 intervention / 5 min ; aucune répétition |
| 2.11 | Commande `com [audience]` | Brouillon utilisable sans réécriture majeure (avis IM) |
| 2.12 | Paramétrage par incident (fréquence des résumés, seuils, réponses dans le chat ou privées) | Modifiable dans l'interface web |

#### 2c — Clôture et mise en production (≈ 2 semaines)

| # | Tâche | Critère d'acceptation |
| --- | --- | --- |
| 2.13 | Commande `clore` + détection fin de réunion → rapport final | Rapport généré < 2 min après clôture |
| 2.14 | Export Markdown/Word, (option) envoi par mail à l'IM | Fichier téléchargeable |
| 2.15 | Rétention/purge, revue sécurité | Revue sécurité OK |
| 2.16 | Dashboards (usage, latence, coûts, feedback) | Dashboard en prod |
| 2.17 | Déploiement recette/prod sur le serveur interne, authentification des IM | Application disponible pour l'équipe IM |
| 2.18 | Pilote en production (4 semaines) | KPI §1 mesurés |

**Jalon : V1 en production.**

#### Options V1 (selon priorités)

- Connecteur ITSM : lecture de l'incident, ajout d'une note de travail après validation de l'IM.
- Lancement automatique de l'agent à la création d'un incident (URL fournie par l'outil ITSM).

### V2 — Partie vocale (à planifier après la V1)

Démarrage conditionné au retour d'expérience de la V1. Tâches pressenties :

1. **Spike** : capture de l'audio de la réunion dans Chromium (pistes WebRTC), qualité de transcription en français (faster-whisper), latence.
2. Validation DPO de la captation de la voix ; annonce aux participants.
3. Retrait du choix « Don't use audio » à la connexion ; transcription continue → messages `source=transcript`.
4. Attribution des locuteurs (indicateur « en train de parler » de Teams, comme Vexa).
5. Fusion chat + oral dans l'état, la timeline et le rapport final.

### Calendrier indicatif

```text
Semaines   1-2          3-5          6-8            9-11            12-13          14+
           Étape 0      Étape 1      Étape 2a       Étape 2b        Étape 2c       Pilote prod
Jalons     ▲Go/No-go    ▲Connecté    ▲Pilote IM     ▲Proactif       ▲Prod V1
V2                                                                                 Spike vocal → V2
```

---

## 8. Stratégie de test et d'évaluation

- **Tests unitaires** : règles proactives, validation de schémas, commandes.
- **Tests d'intégration** contre une **imitation locale de Teams** (`tests/fixtures/fake_teams.html`) : connexion, salle d'attente, refus, fin de réunion, lecture du chat (déjà en place).
- **Test quotidien sur une vraie réunion de référence** pour détecter les changements de l'interface Teams.
- **Étape 1** : tests manuels sur vrais calls (admission, capture complète, messages longs, modifications).
- **Jeu de scénarios « incidents dorés »** (Étape 2) : 10–20 conversations réalistes (anonymisées) d'incidents passés, avec résultats attendus (impact, actions, timeline).
- **Évaluation LLM automatisée** à chaque modification de prompt : complétude des actions extraites, absence d'hallucination (chaque fait traçable à un message), conformité au schéma — LLM-juge + revue humaine sur échantillon.
- **Tests utilisateurs** : calls simulés avec les IM pilotes, grille de notation (utilité, exactitude, bruit).

---

## 9. Structure du repository

```text
/
├── imo_agent.py               # CLI : imo_agent.py join <url>                       (fait)
├── imo/
│   ├── cli.py                 # join, history                                       (fait)
│   ├── agent.py               # enregistrement, commandes imv, accueil              (fait)
│   ├── models.py              # ChatMessage (source chat | transcript)              (fait)
│   ├── sinks.py               # console, JSONL                                      (fait)
│   ├── connectors/teams/      # connexion, sélecteurs, lecture et écriture du chat  (fait)
│   ├── storage/               # SQLite (fait) → PostgreSQL                          (Étape 1)
│   ├── web/                   # FastAPI + interface                                 (Étape 1)
│   ├── analysis.py            # prompts et commandes d'analyse                      (fait)
│   ├── llm/                   # claude-cli, foundry, anthropic, ollama              (fait)
│   ├── incident/              # état incident, extracteur, rapport                  (Étape 2)
│   ├── proactive/             # moteur de règles                                    (Étape 2)
│   └── audio/                 # capture audio + speech-to-text                      (V2)
├── prompts/                   # prompts versionnés + référentiel de questions       (Étape 2)
├── tests/
│   ├── fixtures/fake_teams.html                                                    (fait)
│   └── eval/                  # scénarios dorés + harnais d'évaluation              (Étape 2)
└── docker/                    #                                                     (Étape 1)
```

---

## 10. Risques et mitigations

| Risque | Impact | Mitigation |
| --- | --- | --- |
| Une solution existante (Facilitator, plateforme d'incident) couvre déjà le besoin | Projet inutile ou à réorienter | Tester en Étape 0 (E0.4, E0.5) avant de développer |
| Invités anonymes interdits dans les réunions de l'organisateur | Bloquant : l'agent ne peut pas entrer | Tester en Étape 0 (E0.2) ; repli : extension de navigateur chez l'IM (`ETUDE_FAISABILITE.md` §7) |
| Changement de l'interface Teams cassant les sélecteurs | Agent hors service | Sélecteurs centralisés, test quotidien (1.9), captures de diagnostic |
| Chat fermé aux invités en écriture | L'agent ne peut que lire | Réponses via l'interface web de l'IM |
| Admission oubliée en salle d'attente | Agent absent du call | Rappel à l'IM ; consigne d'admission dans le processus IM |
| Usage non prévu par les conditions Microsoft / refus RSSI | Projet bloqué | Validation juridique et RSSI en Étape 0 (E0.6) |
| Calls ouverts en « appel de groupe » sans objet réunion | Pas de chat de réunion exploitable | Clarifier en Étape 0 (E0.1) ; imposer « Réunion instantanée » ou réunion planifiée |
| Hallucinations / erreurs du LLM | Perte de confiance | Ancrage sur messages sourcés, évaluation continue, feedback 👍/👎 |
| Agent trop bavard | Rejet par les IM | Règles anti-bruit, `pause`, paramétrage |
| Données sensibles envoyées au LLM | Conformité | Masquage, hébergement UE, validation DPO/RSSI |
| Coûts LLM sur calls longs | Budget | État incrémental, résumé glissant, prompt caching, modèle adapté à chaque tâche |
| Langues mixtes FR/EN dans les calls | Qualité | Prompts bilingues, réponse dans la langue de l'IM |
| Qualité de transcription / attribution des locuteurs | V2 moins utile | Spike en début de V2 ; modèle STT adapté au français |

---

## 11. Questions ouvertes

À trancher pour l'Étape 1 :

1. **Comment les calls sont-ils ouverts** aujourd'hui (réunion planifiée, Meet now, appel depuis un chat de groupe, depuis l'outil ITSM) ?
2. Les réunions des IM acceptent-elles les **invités anonymes**, et le chat leur est-il ouvert ?
3. L'agent doit-il **écrire dans le chat** ou répondre seulement à l'IM (interface web) ?

À trancher pour l'Étape 2 :

1. **Fournisseur / hébergement du LLM** autorisé par la sécurité (Microsoft Foundry, API Anthropic, autre) ?
2. **Outil ITSM** utilisé (ServiceNow, Jira SM, autre) et intégration souhaitée en V1 ?
3. **Langue(s)** des échanges : FR uniquement ou FR/EN ?
4. **Durée de rétention** des données et destinataires des rapports ?
5. Volume : nombre de calls par jour / simultanés, durée moyenne ?
6. Existe-t-il un **historique d'incidents** (chats, rapports) exploitable pour constituer le jeu de test ?

À trancher pour la V2 :

1. Le **temps réel sur l'oral** est-il indispensable, ou un rapport enrichi après le call suffit-il ?

---

## 12. Backlog futur

- Suggestions basées sur les **incidents similaires passés** (recherche sémantique sur l'historique / base de connaissance).
- Lien avec le **monitoring** (alertes, dashboards) pour enrichir le contexte.
- **Tableau de bord** des incidents en cours pour le manager IM.
- Génération automatique du **brouillon de post-mortem**.
- Si les API Microsoft deviennent accessibles : bascule possible vers un bot Teams officiel ou Graph (`ETUDE_FAISABILITE.md` §2–3).
