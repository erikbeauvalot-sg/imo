# Contexte IMV — modèle

Copiez ce fichier en `contexte.md`, remplacez les exemples par votre réalité, puis lancez l'agent avec `--context contexte.md` (ou `IMV_CONTEXT=contexte.md` dans `.env`). `contexte.md` n'est pas versionné (`.gitignore`).

Ce texte est envoyé au LLM avec chaque analyse. Il oriente les pistes de résolution et les escalades proposées par IMV.

- Restez factuel et court : une à deux pages suffisent.
- N'y mettez **aucun secret** : ni mot de passe, ni jeton, ni adresse IP sensible.
- Avec `claude-cli`, ce contenu part vers votre compte Claude personnel. Réservez donc ce fournisseur à un contexte non sensible.

---

## Organisation

- Entreprise : Exemple SA, e-commerce, 2 000 salariés, clients en France et en Belgique.
- Plages critiques : 8 h – 22 h, pic de ventes le midi et le soir.
- Process : ITIL. Priorités P1 à P4 ; un P1 déclenche un call de crise et une communication à la direction toutes les 30 minutes.

## Applications et architecture

- Site marchand « Boutique » : frontaux Nginx → API Java (Kubernetes, cluster AKS « prod-weu ») → PostgreSQL 15 (primaire et réplique) ; cache Redis.
- Paiement : prestataire externe PayFast, appelé par l'API « payment-svc ».
- Authentification : Entra ID (SSO) pour les salariés ; Keycloak pour les clients.
- Supervision : Datadog (APM, logs, tableaux de bord « Boutique – Golden signals ») ; alertes dans PagerDuty.
- Déploiements : GitLab CI, mise en production le mardi et le jeudi à 14 h ; retour arrière possible avec `helm rollback`.

## Équipes et escalades

| Domaine | Équipe | Escalade |
| --- | --- | --- |
| Réseau, pare-feu, DNS | Équipe Réseau | astreinte via PagerDuty « network-oncall » |
| Kubernetes, cloud | Équipe Plateforme | astreinte « platform-oncall » |
| Bases de données | DBA | astreinte « dba-oncall » |
| Application Boutique | Équipe Boutique | responsable applicatif : Sophie L. |
| Prestataire paiement | PayFast | support 24/7, contrat Gold, ticket sur leur portail |

## Incidents connus et contournements

- Saturation du pool de connexions PostgreSQL lors des pics : symptôme, erreurs 500 sur l'API avec « too many connections » ; contournement, redémarrer les pods « api » un par un.
- Expiration de certificat sur le frontal Nginx : vérifier la date d'expiration dans le tableau de bord « Certificats ».
- Lenteurs PayFast : consulter d'abord leur page d'état publique avant d'escalader.

## Consignes pour IMV

- Toujours proposer de vérifier les déploiements des dernières 24 h (GitLab, Kubernetes).
- Rappeler la communication à la direction toutes les 30 minutes pour un P1.
