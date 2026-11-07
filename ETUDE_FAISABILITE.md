# Étude de faisabilité et état de l'art — Incident Manager Virtuel (IMV)

> Étape 0 du `PLAN.md`. Version 0.1 — 2026-11-07
> Première passe documentaire (sources publiques). Les points marqués **[À valider]** doivent être confirmés par un PoC ou avec l'admin Microsoft 365.

---

## 1. Synthèse

| Question | Réponse courte | Niveau de confiance |
| --- | --- | --- |
| Un agent peut-il lire **tous** les messages du chat d'une réunion Teams ? | **Oui**, via la permission RSC `ChatMessage.Read.Chat`, documentée par Microsoft | Élevé (doc officielle) — **[À valider]** par PoC sur notre tenant |
| Existe-t-il une solution open source prête à l'emploi ? | **Non**. Aucun projet open source ne couvre « assistant IM dans un call Teams ». Des briques réutilisables existent | Élevé |
| Existe-t-il des solutions du marché ? | **Oui** : Microsoft Facilitator (Copilot), et les plateformes d'incident (incident.io, PagerDuty, FireHydrant) qui ont des agents « scribe » compatibles Teams | Moyen (informations éditeurs) |
| La partie vocale (V2) est-elle faisable ? | **Oui, et plus simple que prévu** : Microsoft ouvre l'accès au transcript **en temps réel** via Work IQ (GA prévue en novembre 2026), en plus du transcript après la réunion via Graph | Moyen — **[À valider]** licences et prérequis |
| Recommandation | **Développer** un agent qui rejoint la réunion **comme participant invité via un navigateur automatisé** (§7), les API Graph n'étant pas accessibles dans la société ; **sauf si** Facilitator ou l'outil d'incident déjà utilisé couvre le besoin | — |

---

## 2. Faisabilité technique — V1 (chat Teams)

### 2.1 Lecture de tous les messages du chat

- Par défaut, un agent ne reçoit que les messages où il est @mentionné.
- La permission RSC **`ChatMessage.Read.Chat`** déclarée dans le manifeste permet de recevoir **tous les messages des utilisateurs** du chat de groupe / de réunion où l'agent est installé ([Microsoft Learn, mise à jour oct. 2026](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-with-rsc)).
- Le **propriétaire du chat** (l'organisateur de la réunion) donne son consentement au moment de l'installation de l'agent.
- La permission ne s'applique qu'après une **installation ou réinstallation** de l'agent dans le chat.

#### Limites identifiées

- Teams **ne transmet pas** à l'agent les messages écrits par d'autres bots (ex. Workflows / Power Automate). Si des alertes sont postées automatiquement dans le chat, il faudra les lire via Graph.
- Microsoft précise que ces permissions ne doivent pas servir à extraire massivement des données clients. Notre usage (assistance pendant le call) est compatible, mais il faut le décrire dans la description de l'app.
- Un retour de développeur (janvier 2026) signale une réception **irrégulière** des messages non mentionnés dans les chats de groupe ([Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/5746183/teamsbot-not-receiving-messages-where-its-not-@men)). C'est un témoignage isolé, mais c'est **le premier point à tester** dans le PoC. **[À valider]**
- Les événements de début / fin de réunion sont montrés dans un exemple Microsoft marqué « public developer preview » ([meetings-events](https://www.github.com/OfficeDev/Microsoft-Teams-Samples/blob/main/samples/meetings-events/csharp)). Il faudra prévoir un repli (commande `clore`). **[À valider]**

### 2.2 Type de call

- Le chat de réunion existe pour les réunions planifiées et les réunions instantanées (« Réunion maintenant »). Un **appel de groupe** lancé depuis un chat utilise le chat de groupe existant : l'agent devrait alors être installé dans ce chat. **[À valider]** avec les usages réels des IM.

### 2.3 SDK

- Microsoft maintient deux SDK : le **Teams SDK** (agents dédiés à Teams) et le **Microsoft 365 Agents SDK** (agents multi-canaux : Teams, Copilot, WebChat…). TeamsFx est déprécié ([Microsoft — choisir un SDK](https://microsoft.github.io/teams-sdk/teams/choosing-an-sdk)).
- Notre agent est **100 % Teams** et utilise des fonctions propres à Teams (réunions, RSC, Adaptive Cards) → **Teams SDK recommandé** (TypeScript). Le passage d'un SDK à l'autre reste possible ensuite.

### 2.4 Prérequis côté tenant (à demander à l'admin M365)

- Autoriser les **applications personnalisées** (sideloading en dev, catalogue d'entreprise en prod).
- Autoriser le **consentement RSC** pour les chats / réunions.
- Créer une **app registration Entra ID** et une ressource **Azure Bot**.

**Conclusion V1 : faisable**, sous réserve des validations du PoC (§6).

---

## 3. Faisabilité technique — V2 (voix)

Le paysage a évolué en 2026 ; trois options existent :

| Option | Principe | Temps réel | Prérequis / limites |
| --- | --- | --- | --- |
| **A. Graph Transcripts API** | Récupération du transcript Teams via Graph | Non (après la réunion) | Accès **désactivé par défaut** au niveau tenant (`-EnableGraphTranscriptAccess`), attribution des locuteurs à activer séparément ; transcription à lancer ([Microsoft Learn](https://learn.microsoft.com/en-us/microsoftteams/meeting-transcript-api-access)) |
| **B. Work IQ — transcript temps réel** | L'agent reçoit le transcript **pendant** la réunion, avec les noms des locuteurs | **Oui** | Réglage « WorkIQ access » désactivé par défaut (effectif au 14/11/2026) ; « Allow AI in meetings » actif ; transcription lancée ; l'app agit **au nom d'un utilisateur connecté** ; Work IQ activé sur le tenant ; GA prévue en novembre 2026 ([roadmap 570651](https://mc.merill.net/message/RM570651)). **Licences [À valider]** |
| **C. Bot participant tiers** | Un bot rejoint le call comme participant et transcrit l'audio (ex. **Vexa**, open source) | Oui | Infra à héberger (Docker, speech-to-text), participant visible dans le call, à faire accepter par la sécurité |

L'ancienne option « bot média Graph Communications » (.NET/Windows) reste possible mais n'est plus nécessaire si B est disponible.

**Conclusion V2 : faisable.** L'option B (Work IQ) rend le temps réel accessible sans infrastructure média. Elle est à vérifier (licences, latence réelle) au lancement de la V2.

---

## 4. État de l'art — solutions existantes

### 4.1 Solution native Microsoft : Facilitator (Copilot)

- Agent Microsoft dans les réunions Teams : notes partagées en direct, suivi de l'ordre du jour, actions et décisions, réponses aux questions dans le chat ; nouvelle fonction qui détecte les questions sans réponse ([MC1409304](https://mc.merill.net/message/MC1409304)).
- **Limites pour notre cas** :
  - générique, **pas spécialisé incident** (pas de checklist ITIL, de relances « impact / owner / prochain point », ni de lien ITSM) ;
  - réunions **planifiées uniquement** (pas les appels, ni, selon certaines sources, les réunions instantanées) ;
  - **licence Microsoft 365 Copilot** requise, Premium pour certaines fonctions (sources contradictoires).
- **Action** : vérifier si l'entreprise dispose de licences Copilot et **tester Facilitator sur 2–3 calls d'incident**. S'il couvre 80 % du besoin, le projet change de nature (complément plutôt que produit complet).

### 4.2 Plateformes de gestion d'incident (commerciales)

| Produit | Ce qu'il fait côté Teams | Remarque |
| --- | --- | --- |
| **incident.io** — Scribe | Rejoint le call d'incident, prend des notes mises à jour en direct, résumés IA dans les canaux Teams ([changelog](https://incident.io/changelog/scribe)) | Suppose d'adopter incident.io comme outil d'incident |
| **PagerDuty** — Scribe Agent | Rejoint les réunions Teams, transcrit dans le chat d'incident ; résumé « wrap me up » à partir du chat ([annonce](https://community.pagerduty.com/announcements-6/scribe-agent-expands-to-microsoft-teams-795)) | Offre PagerDuty Advance |
| **FireHydrant** | Gestion d'incident dans Teams, résumés IA, Scribe avec transcription ([intégration Teams](https://firehydrant.com/integrations/microsoft-teams/)) | Racheté par Freshworks en 2026 (source tierce) |

- **Action** : vérifier si l'entreprise utilise déjà l'un de ces outils, ou si l'outil ITSM en place (ServiceNow, etc.) propose un équivalent (ex. ServiceNow Now Assist).

### 4.3 Open source (GitHub)

| Projet | Contenu | Licence / maturité | Intérêt pour nous |
| --- | --- | --- | --- |
| [**OfficeDev/Microsoft-Teams-Samples**](https://github.com/OfficeDev/Microsoft-Teams-Samples) | Exemples officiels : réception des messages via RSC, transcription de réunion (Node.js et C#), événements de réunion | MIT, maintenu par Microsoft | **Point de départ du code de l'Étape 1** |
| [**Open Incident**](https://github.com/open-incident/open-incident) | Plateforme d'incident avec adaptateurs Slack/Teams et assistant IA (résumé de timeline, brouillons de communication et de post-mortem, masquage avant prompt, « propose sans jamais publier ») | AGPL-3.0, **alpha**, peu d'adoption | Source d'inspiration pour les fonctions IA et les garde-fous. Licence AGPL : ne pas copier de code dans un produit interne sans validation juridique |
| [**IncidentFox**](https://github.com/incidentfox/incidentfox) | « AI SRE » : investigation d'alertes, résumés de cause racine, app Teams | Apache 2.0 (+ BSL), **archivé en mai 2026** | Référence de code (dossier `teams-app`), pas une base de développement |
| [**Vexa**](https://github.com/Vexa-ai/vexa) | Bot de réunion auto-hébergeable (Meet, Teams, Zoom), transcription temps réel avec locuteurs | Apache 2.0, ~3k étoiles, v0.12 | Option C pour la V2 si Work IQ n'est pas utilisable |
| [tldr-summary-assistant](https://github.com/yashvardhanraizada/tldr-summary-assistant) | Bot Teams de résumé de chats (projet de hackathon) | Très peu mature | Aucun |

**Conclusion** : aucun projet open source ne répond directement au besoin. La bonne base est constituée des **exemples officiels Microsoft** et du **Teams SDK** ; Open Incident sert d'inspiration fonctionnelle et Vexa d'option pour la V2.

---

## 5. Build vs Buy — options

| Option | Avantages | Inconvénients |
| --- | --- | --- |
| **1. Facilitator (Copilot)** | Natif, rien à développer, temps réel voix + chat | Générique, licences, réunions planifiées uniquement, peu personnalisable |
| **2. Plateforme d'incident (incident.io, PagerDuty…)** | Scribe spécialisé incident, intégration complète du processus | Changement d'outil, coût, adoption |
| **3. Développement interne (IMV)** | Spécialisé pour notre processus IM, contrôle des données, intégration ITSM sur mesure | Développement et maintenance, dépendance aux API Microsoft |
| **4. Hybride** | Facilitator pour les notes + petit agent IMV pour la checklist incident et les relances | Deux outils dans le call |

**Recommandation provisoire** : option 3, **à confirmer** après le test de Facilitator et la vérification des outils déjà en place (§6, actions E0.4 et E0.5).

---

## 6. Actions restantes de l'étude (Étape 0)

| # | Action | Responsable | Résultat attendu |
| --- | --- | --- | --- |
| E0.1 | Recenser les usages des IM : type de call (réunion planifiée, instantanée, appel de groupe), durée, nombre de participants, bots déjà présents dans le chat | Manager IM | Note d'usages |
| E0.2 | Vérifier les réglages Teams des réunions IM : invités anonymes autorisés, salle d'attente, chat ouvert aux invités (test en navigation privée) | Manager IM | Résultat du test |
| E0.3 | **PoC technique** : `imo_agent.py join <url>` (écrit, §7) sur de vraies réunions, dans les 3 types de call ; ajuster les sélecteurs | Dev | Démo + rapport |
| E0.4 | Tester **Facilitator** sur 2–3 calls d'incident (simulés ou réels) | Manager IM + 2 IM | Grille de couverture du besoin |
| E0.5 | Vérifier les outils déjà en place (ITSM, PagerDuty, incident.io…) et leurs fonctions IA | Manager IM | Liste |
| E0.6 | Premier échange **RSSI / DPO** : captation du chat, envoi à un LLM, hébergement | Manager IM | Avis préliminaire |
| E0.7 | Estimation des coûts (Azure + LLM) pour le volume de calls | Dev | Estimation mensuelle |

### Critères de Go / No-go

- **Go développement** si : le PoC rejoint les réunions et capte de façon fiable tous les messages du chat, les invités anonymes sont autorisés, aucun outil existant (Facilitator, plateforme d'incident) ne couvre l'essentiel du besoin, et le RSSI/DPO ne s'y oppose pas.
- **No-go / pivot** (extension de navigateur, §7) si les invités anonymes sont interdits, ou si Facilitator ou un outil en place couvre le besoin à coût raisonnable.

---

## 7. Décision : approche « participant navigateur » (2026-11-07)

Les §2 et §3 supposent l'accès aux API Microsoft (app Teams avec RSC, ou Graph). **Cet accès n'est pas disponible dans la société** : l'agent passera donc par un **navigateur automatisé** (Playwright + Chromium) qui rejoint la réunion par son URL, comme un invité, puis lit le chat à l'écran. C'est le principe de Vexa et Recall.ai.

| Approche | Autorisation nécessaire | Verdict |
| --- | --- | --- |
| Bot Teams officiel (RSC) | App Teams publiée + consentement RSC | Écartée : non disponible |
| API Graph au nom de l'IM | App Entra ID + consentement Graph | Écartée : non disponible |
| **Participant navigateur** | Aucune autorisation API ; dépend des réglages Teams des réunions | **Retenue** |
| Extension de navigateur chez l'IM | Extensions autorisées par la DSI ; Teams utilisé dans le navigateur | **Repli** si les invités anonymes sont interdits |

### Conditions à vérifier (réglages Teams du tenant organisateur)

- **Invités anonymes autorisés** : activé par défaut, mais le benchmark CIS recommande de l'interdire. Si c'est le cas, Teams redirige vers la connexion et l'agent ne peut pas entrer (code `login_required` du prototype).
- **Salle d'attente** : les invités anonymes y attendent toujours ; un participant doit admettre l'agent.
- **Chat** : si l'option « activé sauf pour les anonymes » est choisie, l'agent lit le chat mais ne peut pas écrire ; il répond alors via l'interface web de l'IM.

### Risques propres à l'approche

- **Fragilité** : chaque évolution de l'interface Teams peut casser les sélecteurs. Il faut un test quotidien et une maintenance régulière.
- **Conditions d'utilisation** : l'automatisation du client Teams web n'est pas un usage prévu par Microsoft. À faire valider (juridique / RSSI).
- **Identité** : l'agent apparaît comme invité « non vérifié ». Son nom et son message d'accueil doivent être explicites.

### Prototype

`imo_agent.py join <url>` est écrit (voir `README.md`). Il couvre la connexion, la salle d'attente, la lecture du chat, la détection de fin de réunion, des codes d'erreur et des captures de diagnostic. Les sélecteurs sont adaptés de Vexa (Apache 2.0, voir `NOTICE`). Il est testé contre une imitation locale de Teams, **mais pas encore sur une vraie réunion** : c'est l'action E0.3.

Pour la V2, l'audio arrive déjà dans ce navigateur : la voix sera captée par le même participant, puis transcrite (§3, option C).

---

## 8. Sources

- [Microsoft Learn — Configure an agent to receive all messages (RSC)](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-with-rsc)
- [Microsoft Q&A — bot ne recevant pas les messages non mentionnés en chat de groupe](https://learn.microsoft.com/en-us/answers/questions/5746183/teamsbot-not-receiving-messages-where-its-not-@men)
- [Microsoft Learn — Manage transcript API access for Teams meetings (Graph et Work IQ)](https://learn.microsoft.com/en-us/microsoftteams/meeting-transcript-api-access)
- [Roadmap M365 570651 — Work IQ: Real Time Transcripts](https://mc.merill.net/message/RM570651)
- [Microsoft — Pick the right SDK for your Teams agent](https://microsoft.github.io/teams-sdk/teams/choosing-an-sdk)
- [Microsoft Learn — SDK comparison](https://learn.microsoft.com/en-ie/microsoftteams/platform/teams-sdk/teams/sdk-comparison)
- [MC1409304 — Facilitator proactively detects and resolves knowledge gaps](https://mc.merill.net/message/MC1409304)
- [HubSite 365 — Use Facilitator Agent](https://www.hubsite365.com/en-ww/crm-pages/run-more-focused-meetings-with-facilitator-agent-in-microsoft-teams.htm)
- [incident.io — Scribe](https://incident.io/changelog/scribe) · [incident.io — Microsoft Teams](https://incident.io/changelog/microsoft-teams)
- [PagerDuty — Scribe Agent expands to Microsoft Teams](https://community.pagerduty.com/announcements-6/scribe-agent-expands-to-microsoft-teams-795)
- [FireHydrant — Microsoft Teams integration](https://firehydrant.com/integrations/microsoft-teams/) · [Better Stack — incident.io vs FireHydrant 2026](https://betterstack.com/community/comparisons/incident-io-vs-firehydrant.md)
- [OfficeDev/Microsoft-Teams-Samples](https://github.com/OfficeDev/Microsoft-Teams-Samples) · [Sample meetings-transcription (Node.js)](https://learn.microsoft.com/en-us/samples/officedev/microsoft-teams-samples/officedev-microsoft-teams-samples-meetings-transcription-nodejs)
- [Open Incident](https://github.com/open-incident/open-incident) · [IncidentFox](https://github.com/incidentfox/incidentfox) · [Vexa](https://github.com/Vexa-ai/vexa) · [tldr-summary-assistant](https://github.com/yashvardhanraizada/tldr-summary-assistant)
