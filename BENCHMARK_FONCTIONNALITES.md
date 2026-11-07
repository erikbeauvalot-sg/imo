# Benchmark des fonctionnalités — outils d'assistance à la gestion d'incident

> Version : 2026-11-07 · Complète `ETUDE_FAISABILITE.md` §4–5 et `PLAN.md` §3.
> Périmètre : fonctions IA liées aux calls / war rooms d'incident (transcription, résumés, catch-up, suggestions, timeline, communications, rapport, incidents similaires, observabilité, garde-fous).
>
> **Légende de fiabilité**
> - **[V]** = vérifié dans une source citée (doc officielle, changelog, annonce éditeur). Les sources tierces sont signalées comme telles.
> - **[D]** = déduit (raisonnement à partir des sources ou de l'architecture), non vérifié.
> - Dans le tableau : ✅ = présent (vérifié) · partiel = présent mais limité ou indirect · ❌ = absent selon la source (ou exclu par l'architecture) · ? = non trouvé / non vérifié.

---

## 1. Synthèse

1. Tous les grands outils (incident.io, PagerDuty, FireHydrant, Rootly, Datadog) ont convergé vers le même socle : **un bot « scribe » qui rejoint le call** (Zoom/Meet/Teams, souvent via Recall.ai), **résumé live**, **catch-up**, **brouillons de communication** et **brouillon de post-mortem**.
2. L'IA est **adossée à une plateforme d'incident** (canal Slack/Teams dédié, timeline, champs structurés, status page). Hors de cette plateforme, la valeur s'effondre.
3. **Teams est un citoyen de seconde zone** : Scribe d'incident.io ne rejoint pas Teams automatiquement et **ne lit pas le chat de réunion Teams** ; PagerDuty ne fait que des mises à jour internes sous Teams ; Teams est en bêta chez FireHydrant et en retard sur Slack chez Rootly.
4. Microsoft Facilitator est le plus proche d'IMV dans Teams (notes live, catch-up, questions ouvertes, tâches), mais il est **générique**, limité aux **réunions planifiées** et soumis à une **licence Copilot**.
5. La **validation humaine** est la norme (partager / éditer / ignorer, approuver / rejeter, « propose sans jamais publier »). Le **masquage des données sensibles** est présent chez incident.io, Rootly et Open Incident.
6. Les tendances 2025-2026 sont l'**investigation « AI SRE »** (logs, métriques, code) et les **incidents similaires**. Ces deux fonctions demandent des intégrations externes et sortent du cœur d'IMV en V1.
7. Aucun outil commercial ne propose ensemble **lecture du chat de réunion Teams sans app**, **hébergement local**, **LLM au choix (y compris local)**, **checklist IM ITIL** et **sorties en français**. C'est la niche d'IMV.

---

## 2. Matrice fonctionnalités × outils

Outils : **Fac** = Microsoft Facilitator (Copilot) · **inc** = incident.io · **PD** = PagerDuty Advance (Scribe Agent…) · **FH** = FireHydrant · **Rly** = Rootly · **DD** = Datadog Incident Mgmt / Bits AI · **SN** = ServiceNow Now Assist ITSM · **OI** = Open Incident (OSS) · **IFx** = IncidentFox (OSS, archivé) · **Vx** = Vexa (OSS, bot de transcription) · **IMV** = état actuel ou prévu (pour comparaison).

| Fonctionnalité | Fac | inc | PD | FH | Rly | DD | SN | OI | IFx | Vx | IMV |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Bot qui rejoint le call et transcrit la voix en direct | ✅ (natif) | ✅ (Teams : ajout manuel) | ✅ | ✅ (Teams depuis mai 2026) | ✅ | ✅ | ? | ? | ? | ✅ | V2 |
| Lecture du **chat de la réunion** Teams | ✅ | ❌ (doc explicite) | ? | ? | ? | ? | partiel (import de transcript de chat dans le ticket) | ? | ? | ? | ✅ V1 |
| Résumé / notes live ou périodiques | ✅ | ✅ | partiel (en fin de réunion) | ✅ | ✅ | ✅ (~10 min) | ✅ (fiche incident) | ✅ | partiel | ❌ | ✅ (à la demande) ; 30 min prévu |
| Catch-up pour un nouvel arrivant | ✅ | ✅ (sujet courant, moments clés) | ✅ (Slack) | ✅ | ✅ (`/rootly catchup`) | ✅ (à l'arrivée dans le canal) | partiel (résumé de réassignation) | ? | ? | ❌ | ✅ |
| Détection des décisions et actions | ✅ | ✅ (moments clés + follow-ups) | ? | partiel (follow-ups dans la rétro) | partiel (décisions dans le résumé) | ? | ? | ✅ (suggestions de follow-up) | ? | ❌ | ✅ (`actions`) |
| Questions ouvertes / suggestions de questions | ✅ (questions ouvertes, lacunes de connaissance) | partiel (relances par règles, prochaines étapes) | partiel (suggestions de dépannage) | ? | partiel (prochaines étapes AI SRE) | ? | partiel (étapes suggérées) | ? | partiel | ❌ | ✅ (`questions`) |
| Timeline horodatée | ? | ✅ | partiel (dans le PIR) | ✅ | ✅ | ✅ (résumés versés à la timeline) | ? | ✅ | ? | ❌ | ✅ |
| Brouillon de communication interne | partiel (documents Word) | ✅ | ✅ (Teams : interne seulement) | ✅ | ✅ | ✅ (modèles avec champs IA) | ✅ (e-mail d'incident majeur) | ✅ | ? | ❌ | prévu (`com`) |
| Brouillon de mise à jour de status page publique | ❌ | ? | partiel (Slack seulement) | ✅ | ? | ✅ | ? | ? | ? | ❌ | ❌ |
| Brouillon de post-mortem / rapport | partiel (Word) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (agent PIR) | ✅ | partiel (RCA) | ❌ | prévu (`clore`) |
| Follow-ups oubliés détectés à la résolution | ? | ✅ | ? | partiel | ? | ? | ? | ✅ | ? | ❌ | — |
| Incidents similaires | ❌ | ✅ | partiel (mémoire de l'agent SRE) | ✅ | ✅ | ✅ (corrélation sur 20 min + recherche dans l'historique) | partiel | ✅ | ? | ❌ | backlog |
| Investigation par l'observabilité (logs, métriques, code) | ❌ | ✅ | ✅ (SRE Agent) | ? | ✅ (AI SRE) | ✅ (Bits Investigation) | partiel | ❌ | ✅ | ❌ | backlog |
| Q&A libre sur l'incident | ✅ | ✅ | ✅ | ? | ✅ (Ask Rootly AI) | ✅ | ✅ | ? | ✅ | partiel (MCP) | — |
| Actions sur l'incident depuis le chat (sévérité, rôles, statut) | partiel (tâches Planner) | ? | ? | ? | ✅ (rôles, mise à jour après confirmation) | ✅ (sévérité, statut) | ? | ✅ (commandes, hors IA) | ? | ❌ | partiel (`démarrer`, `pause`) |
| Interventions proactives | ✅ | ✅ (suggestions poussées) | partiel (Insights Agent) | ✅ (incidents liés) | ? | ✅ | ? | ? | ✅ (auto-investigation) | ❌ | prévu (2b) |
| Validation humaine explicite (éditer / ignorer / approuver) | ? | ✅ | partiel (consigne de vérification) | ? | ✅ | ✅ | ✅ (boucle de revue) | ✅ | ? | n/a | prévu |
| Masquage des données sensibles avant le LLM | ? | ✅ | ? | ? | ✅ (26 catégories) | ? | ? | ✅ | ? | n/a | à faire |
| Auto-hébergement / LLM au choix | ❌ | ❌ | ❌ | ❌ | partiel (BYOK sur certaines fonctions) | ❌ | ? | ✅ | ✅ (24 fournisseurs) | ✅ | ✅ |
| Animation de réunion (agenda, minuteurs, dépassement) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | — |
| Sorties dans la langue du call | ? | ❌ (résumés en anglais) | ? | ? | ❌ (résumé en anglais) | ? | ? | ? | ? | ? | ✅ (français) |

Note [D] : les ❌ « animation de réunion » des outils incident signifient « non trouvé et hors de leur positionnement », pas une absence prouvée.

---

## 3. Fiches par outil

### 3.1 Microsoft Facilitator (Microsoft 365 Copilot)
- **Fonctions IA [V]** : notes live co-éditables ; résumé des décisions et des questions ouvertes ; réponses dans le chat ; catch-up sur demande (@Facilitator) ; documents Word tirés de la réunion ; agenda avec minuteurs ; alerte de dépassement d'horaire et aide à planifier le suivi ; @mention des absents cités plusieurs fois ; tâches Planner (préversion) ; alerte de **phishing vocal** si des externes sont présents (préversion) ; détection proactive des lacunes de connaissance (MC1409304).
- **Teams** : natif.
- **Limites [V]** : réunions **planifiées** uniquement (pas les réunions instantanées, les réunions de canal ni les appels) ; licence Copilot requise pour l'activer ; transcription nécessaire pour les questions après la réunion ; ne lit pas les invitations chiffrées ou étiquetées.
- **Points marquants** : générique, rien de propre à l'incident. Pas de rapport d'incident, d'ITSM ni de checklist IM [D].

### 3.2 incident.io (Scribe + AI)
- **Fonctions IA [V]** : Scribe transcrit le call, tient un résumé live avec prochaines étapes, affiche le **sujet en cours** et publie les **moments clés** (décisions, nouvelles informations) dans le canal. Suggestions de **mises à jour**, de **résumés** et de **follow-ups** (à la résolution, il repère les follow-ups discutés mais pas créés). Titre et résumé générés à la déclaration. Investigations (code, tableaux de bord), incidents similaires, brouillon de post-mortem (timeline, facteurs contributifs, follow-ups).
- **Déclenchement [V]** : proactif (relance sur l'activité, suggestion après une mise à jour) ou à la demande (`@incident draft me a new update`). Validation : *Share / Edit before sharing / Dismiss*.
- **Teams [V]** : oui, mais Scribe ne rejoint **pas automatiquement** les réunions Teams (ajout manuel) et **ne lit pas le chat de réunion Teams**.
- **Confidentialité [V]** : masquage des données avant l'IA ; sous-traitants Recall.ai et ElevenLabs, sans entraînement ; enregistrement désactivé par défaut. Plus de 90 langues transcrites, mais résumés **en anglais**.

### 3.3 PagerDuty Advance (Scribe Agent, SRE Agent, Insights Agent)
- **Fonctions IA [V]** : Scribe Agent rejoint automatiquement les réunions Teams (déc. 2025) et Zoom, transcrit dans le chat d'incident, produit un résumé en fin de réunion et des brouillons de mise à jour de statut. Assistant Slack/Teams : résumé, causes possibles, astreinte, suggestions de dépannage. Résumés catch-up et wrap-up (Slack). Brouillon de post-incident review (Scribe + Slack + journal d'incident). SRE Agent (diagnostic, remédiations approuvées, mémoire des incidents). Insights Agent (recommandations proactives, accès anticipé). Runbooks générés (accès anticipé).
- **Teams [V]** : oui, mais les mises à jour **externes** sont réservées à Slack ; Teams et le web n'ont que les mises à jour internes.
- **Garde-fous [V]** : l'utilisateur doit vérifier les sorties. Fournisseur de LLM et politique d'entraînement non précisés dans la page consultée (?).

### 3.4 FireHydrant (Freshworks)
- **Fonctions IA [V]** : résumés d'incident pour les nouveaux arrivants ; contexte tiré des transcriptions Zoom ; Scribe sur Teams (mai 2026) en direct ou après la réunion ; **incidents liés suggérés de façon proactive** ; brouillon de rétrospective avec follow-ups ; brouillons de status page **internes et publics**.
- **Teams [V]** : intégration Teams en **bêta**. L'IA utilise la timeline comme contexte et peut être désactivée.
- **Points marquants** : IA activable par organisation et par fonction. Fournisseurs « trusted AI partners » non nommés. Selon une source tierce, l'IA est réservée à l'offre Enterprise.

### 3.5 Rootly (ajout)
- **Fonctions IA [V]** : Meeting Scribe via Recall.ai (Zoom, Meet, **Teams**, Webex, GoTo), transcription live avec locuteurs, résumé des points clés et des décisions, `/rootly catchup` alimenté par le transcript live, Ask Rootly AI (lecture seule), AI SRE (investigation, prochaines étapes), incidents liés avec la raison du rapprochement, rétrospectives rédigées avec sources visibles, Rootly AI dans Slack (pager, rôles, comms).
- **Garde-fous [V]** : la plupart des fonctions **suggèrent sans modifier** l'incident ; confirmation sur mobile ; **masquage PII** de 26 catégories avant stockage ; nettoyage des prompts ; pas d'entraînement cross-client ; **BYOK** sur titres, résumés et catch-up. Résumé en anglais.
- **Teams [V]** : canaux, workflows et pagination dans Teams. Commandes IA côté Teams non documentées (?) ; selon une source tierce, Teams est en retard sur Slack.

### 3.6 Datadog Incident Management / Bits AI (ajout)
- **Fonctions IA [V]** : résumé proactif à l'arrivée dans le canal, résumé final à la résolution ; `/dd incident summary` (visible du seul demandeur) ; changement de sévérité ou de statut en langage naturel ; brouillon de post-mortem ; Datadog Transcriber qui rejoint Zoom, Meet et **Teams** et publie un résumé **environ toutes les 10 min** dans la timeline et les canaux ; incidents liés (déclarés à moins de 20 min d'écart) ; Bits Investigation (traces, métriques, logs) ; brouillon de status page ; modèles de notification à champs IA.
- **Validation [V]** : chaque résumé de réunion peut être approuvé, édité ou rejeté, avec traçabilité de l'auteur.
- **Points marquants** : très fort sur l'observabilité, mais suppose d'être client Datadog [D].

### 3.7 ServiceNow Now Assist for ITSM (ajout, ITSM déjà cité)
- **Fonctions IA [V]** : résumé d'incident, résumé de discussion Sidebar, résumé de réassignation, notes de résolution, article de connaissance, étapes suggérées à partir d'incidents similaires résolus, **brouillon d'e-mail d'incident majeur** selon un modèle, Q&A sur l'incident, agent **Post Incident Review** pour les incidents majeurs (résumé exécutif, impact, détail technique, actions, avec boucle de revue jusqu'à approbation).
- **Teams [V]** : gestion d'incidents majeurs depuis Teams, chat lancé depuis le ticket et transcript importé. Résumé des messages Teams selon un post communautaire (non officiel). Pas de bot vocal trouvé (?).
- **Points marquants** : alimenté par les **enregistrements ITSM**, pas par le call. Complémentaire d'IMV si l'entreprise utilise ServiceNow [D].

### 3.8 Open Incident (OSS, AGPL, alpha)
- **Fonctions IA [V]** : titre et résumé à la déclaration, résumé de timeline, incidents similaires, brouillons de mise à jour, de follow-up et de post-mortem, usage des runbooks. **« Propose, never publishes »**, sorties étiquetées, masquage avant prompt, journal lisible des appels, fonctions marquées indisponibles si aucun LLM n'est configuré. Tout endpoint compatible OpenAI. Docker Compose.
- **Teams [V]** : appairage par code, une équipe par espace de travail. Projet alpha testé sur des mocks.

### 3.9 IncidentFox (OSS, archivé le 31/05/2026)
- **Fonctions IA [V]** : auto-investigation d'alertes avec résumé de cause racine, corrélation d'alertes, plus de 45 intégrations (Datadog, Grafana, Prometheus, Splunk, PagerDuty…), 24 fournisseurs de LLM (dont Ollama), auto-hébergeable, Slack, Teams ou Google Chat.
- **Intérêt** : référence pour le multi-LLM et les connecteurs d'observabilité. Ne pas en faire une base (projet archivé).

### 3.10 Vexa (OSS, Apache 2.0, v0.12)
- **Fonctions [V]** : bot Meet, Teams et Zoom ; transcription Whisper temps réel avec attribution du locuteur (laissée vide si incertaine, 4 à 7 % des lignes) ; API REST et endpoint MCP ; auto-hébergement air-gapped (Docker, Helm). Prise de parole (TTS) indisponible en open-core.
- **Intérêt** : brique candidate pour la **V2 voix** d'IMV, pas un assistant IM.

---

## 4. Fonctionnalités candidates pour IMV (priorisées)

Faisabilité : **Chat** = réalisable avec le chat seul (V1) · **Voix** = nécessite la V2 · **Ext** = nécessite une intégration externe (ITSM, monitoring, historique).
Les « outils qui l'ont » renvoient au tableau §2 et sont vérifiés. La valeur et la faisabilité pour IMV sont **déduites [D]**.

| # | Fonctionnalité | Description | Valeur pour l'IM | Faisabilité | Phase | Outils qui l'ont |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **Brouillons de communication par audience** (`com mgmt / users / tech`) | Brouillon structuré (impact, statut, prochaine étape, heure du prochain point) avec modèles configurables. Le bouton « éditer / copier » vit dans l'interface web | Le plus gros gain de temps en incident majeur | Chat | 2b (prévu) | inc, PD, FH, Rly, DD, SN, OI |
| 2 | **Moments clés en continu** (décisions, hypothèses validées ou invalidées, actions lancées) | Détection incrémentale à chaque nouveau message, avec un fil de « moments clés » dans l'interface web. Publication dans le chat optionnelle | Alimente la timeline, le catch-up et le rapport sans effort | Chat (meilleur avec la voix) | 2b | inc, Fac, Rly |
| 3 | **Suggestions proactives à validation humaine** (Partager / Éditer / Ignorer) | Chaque suggestion arrive d'abord en privé dans l'interface web de l'IM, puis est publiée dans le chat s'il l'accepte. Anti-bruit prévu | Proactivité sans polluer le call ; l'IM garde la main | Chat | 2b | inc, DD, Rly, OI, SN |
| 4 | **Résumé périodique automatique** | Mini-point toutes les N min (Datadog : environ 10 min), désactivable, sauté si rien n'a changé | Tient le call aligné ; prépare la communication | Chat | 2b (prévu à 30 min) | DD, Fac, inc |
| 5 | **Rapport de clôture et brouillon de post-mortem** | Résumé exécutif, impact, timeline, cause, actions, points de PIR, avec sources (message de référence) et sections éditables | Livrable attendu de l'IM | Chat | 2c (prévu) | inc, PD, FH, Rly, DD, SN, OI |
| 6 | **Follow-ups oubliés à la clôture** | À `clore`, repérer les actions évoquées sans owner ni ticket et les proposer | Évite les pertes d'actions, un défaut classique | Chat | 2c | inc, OI |
| 7 | **Masquage des données sensibles avant le LLM, et alerte de secret collé** | Expressions régulières et listes (e-mails, téléphones, IP, mots de passe, jetons) remplacées avant envoi au LLM, avec avertissement si un secret est posté dans le chat | Condition d'acceptabilité sécurité/RGPD ; différenciant pour les LLM externes | Chat | 2a/2b | inc, Rly, OI (masquage) ; Fac (alerte phishing, voisin) |
| 8 | **Q&A libre sur l'incident** (`imv ? <question>`) | Réponse fondée sur le chat, avec citation des messages | Flexibilité ; couvre les besoins non prévus | Chat | 2a | Fac, inc, PD, Rly, DD, SN |
| 9 | **Champs structurés et suggestion de sévérité** | Extraire et maintenir impact, services, début, détection, mitigation, sévérité proposée selon la grille interne. Alimente `statut` | Fiabilise la checklist et les communications | Chat | 2a/2b | DD (sévérité via le chat), inc (titre et résumé) |
| 10 | **Rôles du call** (IC, comms lead, experts, owners) | Déduire les rôles depuis le chat, `imv rôles`, relancer si aucun comms lead n'est désigné | Clarifie qui fait quoi | Chat | 2b | Rly (attribution des rôles) |
| 11 | **Catch-up ciblé « depuis HH:MM » ou pour une personne** | `imv catchup 14:10` ou `catchup @nom` (depuis son premier message) | Plus précis que le catch-up global | Chat | 2a | Rly, DD, FH, inc |
| 12 | **Rappel de cadence de communication et minuteur** | « Prochain point à 15:30 », alerte 5 min avant, relance si dépassé | Discipline de communication | Chat | 2b (partiellement prévu) | Fac (minuteurs, dépassement) |
| 13 | **Détection des personnes citées mais absentes** | « X est cité 3 fois mais n'est pas dans le call : le contacter ? » | Accélère l'escalade | Chat + liste des participants (à vérifier) | backlog | Fac |
| 14 | **Titre et résumé auto au `démarrer`** | Proposer un titre d'incident et un résumé initial à partir des premiers messages | Démarrage rapide, nommage homogène | Chat | 2a | inc, OI |
| 15 | **Incidents similaires (historique local IMV)** | Recherche sémantique sur les sessions et rapports IMV passés (dossier `sessions/`), puis sur l'ITSM | Réutiliser les résolutions connues | Chat + historique (Ext léger) | backlog (après 2c) | inc, FH, Rly, DD, OI, SN (partiel) |
| 16 | **Note de travail et rapport poussés dans l'ITSM** | Work notes, champ PIR, timeline | Évite la double saisie | Ext | option V1 | SN (PIR dans la fiche), PD, inc |
| 17 | **Transcription, moments clés oraux et sujet en cours** | Fusion chat et voix ; « sujet en cours » affiché | Capte l'essentiel, qui se dit à l'oral | Voix | V2 (prévu) | inc, PD, FH, Rly, DD, Fac, Vx |
| 18 | **Brouillon de status page / communication externe** | Variante de `com` pour les clients externes, avec avertissement renforcé | Utile si l'IM gère la communication client | Chat (publication : Ext) | backlog | FH, DD, PD (Slack) |
| 19 | **Enrichissement par l'observabilité** | Alertes et tableaux de bord liés au service impacté | Contexte technique | Ext | backlog | DD, inc, PD, Rly, IFx |
| 20 | **Journal d'audit des appels LLM** | Prompt masqué, modèle, coût, sortie, décision de l'IM | Traçabilité, conformité, mise au point des prompts | Chat | 2c | OI, DD (traçabilité des approbations) |

---

## 5. Idées différenciantes pour IMV

Ce que les outils commerciaux font mal ou ne font pas, d'après les sources (§3) ; la portée pour IMV est déduite [D].

1. **Teams natif sans app ni droits d'administration** : IMV rejoint comme invité navigateur, sans app Teams ni consentement Graph. Les plateformes exigent l'installation d'une app ou d'un bot dans le tenant [D] ; Facilitator exige une licence Copilot et une réunion planifiée [V].
2. **Lecture du chat de réunion** : c'est le cœur de la V1, alors que incident.io indique explicitement ne pas capter le chat de réunion Teams [V]. Les autres scribes reposent sur l'audio [V].
3. **Toutes réunions** : réunions instantanées et ponts d'incident ad hoc, là où Facilitator est limité aux réunions planifiées [V] (sous réserve des réglages invités du tenant).
4. **Hébergement local et LLM au choix, y compris local (Ollama, etc.)** : aucun des 6 commerciaux étudiés ne le propose (Rootly : BYOK partiel) [V]. Argument fort pour les incidents qui touchent des données sensibles.
5. **Spécialisé processus IM / ITIL** : checklist impact / owner / prochain point, grille de sévérité interne, modèles de communication de l'entreprise. Les outils sont génériques (Facilitator) ou orientés SRE/DevOps (incident.io, Rootly, Datadog) [D].
6. **Sorties en français** : incident.io et Rootly produisent leurs résumés en anglais [V].
7. **Indépendant de la plateforme** : pas besoin de migrer vers incident.io ou PagerDuty. IMV est complémentaire de l'ITSM existant (ServiceNow, etc.) [D].
8. **Mode privé pour l'IM** : les suggestions restent dans l'interface web locale jusqu'à validation. Les outils publient en général dans le canal partagé (Datadog : résumé à la demande visible du seul demandeur) [V/D].
9. **Coût marginal** : pas de licence par répondant, seulement des tokens LLM, ou rien en local [D].
10. **Transparence** : citation des messages sources dans chaque sortie et journal d'audit local, sur le modèle d'Open Incident et des rétrospectives Rootly [V pour eux, D pour IMV].

---

## 6. Sources

**Microsoft Facilitator**
- [Support Microsoft — Facilitator in Microsoft Teams meetings](https://support.microsoft.com/office/facilitator-in-microsoft-teams-meetings-37657f91-39b5-40eb-9421-45141e3ce9f6)
- [MC1409304 — Facilitator proactively detects and resolves knowledge gaps](https://mc.merill.net/message/MC1409304)
- [topedia — Facilitator GA (tierce)](https://blog-en.topedia.com/2025/07/facilitator-agent-in-teams-meetings-reaches-general-availability/)

**incident.io**
- [Changelog Scribe](https://incident.io/changelog/scribe) · [Docs Scribe](https://docs.incident.io/ai/scribe) · [Docs Managing Scribe (Teams)](https://docs.incident.io/ai/managing-scribe)
- [Docs AI suggestions](https://docs.incident.io/ai/suggestions) · [AI platform](https://incident.io/ai-platform) · [Postmortems](https://incident.io/postmortems)

**PagerDuty**
- [Scribe Agent expands to Microsoft Teams (déc. 2025)](https://community.pagerduty.com/announcements-6/scribe-agent-expands-to-microsoft-teams-795)
- [Docs PagerDuty Advance](https://docs.pagerduty.com/ai-automation/advance)
- [Communiqué AI Agent Suite (oct. 2025)](https://www.businesswire.com/news/home/20251008279706/en/PagerDuty-Launches-Industry%E2%80%99s-First-End-to-End-AI-Agent-Suite-Slashing-Incident-Response-Times-and-Empowering-Teams-to-Innovate) · [What's new](https://www.pagerduty.com/whats-new/)

**FireHydrant**
- [Docs AI-Powered Incident Management](https://docs.firehydrant.com/docs/ai-powered-incident-management) · [AI Meeting Transcription](https://docs.firehydrant.com/docs/ai-meeting-transcription) · [AI-Drafted Retrospectives](https://docs.firehydrant.com/docs/ai-drafted-retrospectives)
- [Changelog avril 2026 (Scribe Teams)](https://firehydrant.com/changelog/april-recap-eu-instance-ms-teams-scribe-and-more/) · [Intégration Microsoft Teams](https://firehydrant.com/integrations/microsoft-teams/)
- [Better Stack — FireHydrant vs Rootly 2026 (tierce)](https://betterstack.com/community/comparisons/firehydrant-vs-rootly.md)

**Rootly**
- [Docs Rootly AI](https://docs.rootly.com/ai/ai) · [Meeting Scribe](https://docs.rootly.com/ai/meeting-scribe) · [Related incidents](https://docs.rootly.com/ai/related-incidents)
- [Intégration Microsoft Teams](https://docs.rootly.com/integrations/microsoft-teams) · [Blog — Knowledge reinforcement (oct. 2025)](https://rootly.com/sre/rootly-knowledge-reinforcement-captures-postmortem-insights)

**Datadog**
- [Docs Bits AI — Managing incidents](https://docs.datadoghq.com/bits_ai/managing_incidents)
- [Blog — Incident response AI features](https://datadoghq.com/blog/datadog-incident-response-ai-features/) · [Blog — Bits AI for incident management](https://datadoghq.com/blog/bits-ai-for-incident-management)

**ServiceNow**
- [Using Now Assist for ITSM (Zurich)](https://www.servicenow.com/docs/bundle/zurich-it-service-management/page/product/now-assist-itsm/concept/using-now-assist-for-itsm.html)
- [Agentic workflow — Generate post incident reviews](https://www.servicenow.com/docs/r/it-service-management/now-assist-for-it-service-management-itsm/now-assist-itsm-aiagents-mim-usecase.html)
- [ServiceNow for Microsoft Teams and Microsoft 365](https://www.servicenow.com/docs/r/zurich/employee-service-management/employee-experience-foundation/c_ServiceNowForMSTeams.html)
- [Community — chat summarization for Teams (non officiel)](https://www.servicenow.com/community/incident-management-forum/how-to-enable-chat-summarization-for-teams/td-p/3535026)

**Open source**
- [Open Incident](https://github.com/open-incident/open-incident) · [IncidentFox](https://github.com/incidentfox/incidentfox) · [Vexa](https://github.com/Vexa-ai/vexa)
