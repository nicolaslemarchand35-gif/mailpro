# Assistant mail professionnel

Application web privée qui lit la boîte **Exchange OVH** (EWS), trie les mails en
**À traiter** / **À savoir**, propose des **tâches**, prépare des **brouillons de réponse**
dans Exchange et propose un **classement** dans les dossiers Outlook réels.

**L'assistant n'envoie jamais de mail.** Les brouillons sont enregistrés dans le dossier
Brouillons : vous les relisez et les envoyez vous-même depuis Outlook.

Hébergement prévu (gratuit, sans carte bancaire) : **Render Free** (application) +
**Firebase Firestore Spark** (données) + **cron-job.org** (déclenchement toutes les 10 min) +
**OpenRouter** (modèle IA gratuit).

> Le brief de passation d'origine est conservé dans `docs/brief-passation.md`.

---

## Ce que fait l'application

| Écran | Contenu |
|---|---|
| **À traiter** | Demandes, décisions, échéances, blocages. Priorité, échéance (écrite dans le mail ou suggérée), projet, « Répondu le … » si une réponse a été envoyée dans le fil. |
| **Brouillons** | Réponses proposées par l'IA, modifiables, puis enregistrées dans Brouillons Exchange (réponse dans le fil d'origine, signature ajoutée). Un brouillon modifié dans Outlook n'est jamais écrasé. |
| **À savoir** | Informations sans réponse nécessaire (comptes rendus, confirmations, copies). Archivées après 10 jours (réglable). |
| **Tâches** | Création manuelle ou proposée par l'IA (à accepter/rejeter) : projet, priorité, échéance et sa source, statut, mails sources. |
| **Classement** | Propositions de dossier, déplacement en un clic, création de dossier, journal des opérations avec **annulation**. |
| **Réglages** | Horaires, dossiers suivis, IA, signature, style, interlocuteurs prioritaires, projets (mots-clés, interlocuteurs, dossier cible), conservation, automatismes. |

Principes métier appliqués :

- l'emplacement d'un mail et son état de traitement sont séparés (un mail rangé peut rester à traiter) ;
- les mails envoyés sont lus : une réponse déjà faite est signalée et transmise à l'IA ;
- une échéance « écrite » doit être citée textuellement dans le mail, sinon elle devient « suggérée » ;
- le contenu des mails est traité comme une **donnée non fiable** : la sortie de l'IA est validée
  (catégories, projets et dossiers limités aux listes connues) et ne déclenche aucun envoi ;
- automatismes (brouillons et déplacements automatiques) **désactivés par défaut** ; le déplacement
  automatique exige en plus un projet marqué « déplacement automatique », un dossier cible, une
  correspondance de mot-clé/interlocuteur validé et une confiance minimale.

## Fiabilité de la synchronisation

- **Synchronisation incrémentale** `SyncFolderItems` avec un curseur par dossier suivi (boîte de
  réception par défaut, + les dossiers alimentés par vos règles Outlook) et par Éléments envoyés.
- **Historique initial borné** (7 jours par défaut) ; **rattrapage** automatique après une absence
  (vacances, service endormi) : rien n'est ignoré depuis la dernière synchro complète.
- **Lots courts** (budget de 24 s par passage, sous la limite de 30 s de cron-job.org) ; le travail
  restant reprend au passage suivant. Le curseur n'avance qu'après l'écriture des messages
  (identifiants déterministes : pas de doublon si un passage est interrompu).
- **Verrou distribué** Firestore avec bail d'expiration : deux passages simultanés ne se chevauchent pas,
  un passage interrompu libère le verrou à expiration.
- **Brouillons** : marqués d'une propriété de corrélation Exchange ; une création interrompue est
  réconciliée avant toute nouvelle création (pas de doublon).
- **Quota IA** : limite quotidienne réglable (45 par défaut), pause de 15 min si le fournisseur
  répond « quota atteint » ; les mails restent visibles avec une analyse par règles, marqués
  « Analyse IA en attente ». Après 3 échecs, l'analyse par règles est conservée (relance manuelle possible).
- **Statut visible** en haut de l'écran : dernière synchro réussie, mails en attente d'analyse,
  erreurs, alerte si aucune synchro réussie depuis 35 min pendant la plage de travail.

---

## Mise en place (pas à pas)

Comptez environ une heure. Aucun mot de passe ne doit être écrit dans le code ou dans un chat :
ils se saisissent uniquement dans les écrans « Environment » de Render.

### 1. Dépôt GitHub

Le code est dans ce dépôt (privé). Render le déploiera directement depuis GitHub.

### 2. Firebase (données + connexion)

1. <https://console.firebase.google.com> → **Ajouter un projet** (ex. `assistant-mail`), Google Analytics inutile.
   Restez sur le forfait **Spark** (gratuit) ; n'ajoutez pas de compte de facturation.
2. **Firestore Database** → Créer → mode **production** → région UE (ex. `eur3` ou `europe-west9` Paris).
   La région ne peut plus être changée ensuite.
3. Onglet **Règles** de Firestore : collez le contenu du fichier `firestore.rules` (tout est fermé ;
   seul le serveur accède aux données) → Publier.
4. **Authentication** → Commencer → **E-mail/Mot de passe** → Activer.
   Onglet **Users** → **Ajouter un utilisateur** : votre adresse et un mot de passe solide.
   Copiez l'**UID** affiché (colonne « UID utilisateur »).
5. ⚙️ **Paramètres du projet** → **Général** → « Vos applications » → icône **Web `</>`** → nom
   `assistant-mail` (sans Hosting). Copiez l'objet `firebaseConfig` et gardez seulement
   `apiKey`, `authDomain`, `projectId`, sous forme JSON, par exemple :
   `{"apiKey":"AIza...","authDomain":"assistant-mail.firebaseapp.com","projectId":"assistant-mail"}`
   (ces valeurs sont publiques par nature, ce ne sont pas des secrets).
6. ⚙️ **Paramètres du projet** → **Comptes de service** → **Générer une nouvelle clé privée**.
   Un fichier JSON est téléchargé : c'est un **secret** (ne le mettez jamais dans le dépôt).

### 3. OpenRouter (IA)

1. Créez un compte sur <https://openrouter.ai> puis une clé : **Keys** → Create key.
2. **Settings → Privacy** : les modèles gratuits ne sont disponibles que si vous autorisez les
   fournisseurs gratuits ; certains peuvent **conserver ou utiliser les prompts pour l'entraînement**.
   Les mails envoyés à l'IA contiennent des informations professionnelles : vérifiez que votre
   entreprise l'autorise. Option plus stricte : `OPENROUTER_NO_TRAINING=true` (exclut ces
   fournisseurs, mais peut rendre les modèles gratuits indisponibles).
3. Limites gratuites constatées à ce jour (à revérifier sur openrouter.ai, elles changent) :
   environ 20 requêtes/minute et **50 requêtes/jour** sur les modèles `:free` tant que le compte
   n'a pas acheté au moins 10 $ de crédits (1 000/jour ensuite). L'application fait **un appel par
   mail** et s'arrête à 45 par jour (réglable) ; les newsletters ne sont pas envoyées à l'IA.

Modèle par défaut : `nvidia/nemotron-3.5-lightning:free`, avec repli sur `openrouter/free`
(routeur qui choisit un modèle gratuit disponible). Sur 6 mails fictifs de chantier
(`python -m scripts.eval_models`), il a classé correctement 6 sur 6 en environ 4 s par mail, sans
suivre la tentative d'injection ; `openrouter/free` seul a fait 4 sur 6 en environ 9 s. **Limite
constatée** : les petits modèles gratuits formulent parfois un engagement dans les brouillons
(« nous envoyons une équipe ») malgré la consigne contraire. Relisez toujours avant d'envoyer.
Pour changer de modèle, modifiez simplement `OPENROUTER_MODEL` sur Render (liste séparée par
des virgules = ordre de repli).

### 4. Render (hébergement)

1. <https://dashboard.render.com> → **New** → **Blueprint** → choisissez ce dépôt GitHub.
   Le fichier `render.yaml` crée le service `assistant-mail` (Free, Francfort).
2. Dans **Environment**, renseignez les variables marquées « à saisir » :

| Variable | Valeur |
|---|---|
| `OVH_EMAIL` | votre adresse mail complète |
| `OVH_PASSWORD` | votre mot de passe de messagerie (secret) |
| `ADMIN_API_TOKEN` | générée automatiquement par Render (diagnostic technique) |
| `CRON_SECRET` | générée automatiquement par Render (déclencheur) — différente de la précédente |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | **tout le contenu** du fichier JSON de l'étape 2.6 |
| `FIREBASE_WEB_CONFIG` | le JSON de l'étape 2.5 |
| `ALLOWED_UIDS` | l'UID de l'étape 2.4 |
| `OPENROUTER_API_KEY` | la clé de l'étape 3.1 |
| `FIRESTORE_ENABLED` | `false` pour le tout premier test, puis `true` |

3. Déployez. Vérifiez <https://VOTRE-SERVICE.onrender.com/healthz> → `{"status":"up"}`.
   (Cette réponse prouve seulement que le serveur tourne, pas qu'une synchronisation a réussi.)

### 5. Premier test de connexion OVH depuis Render (étape clé, jamais testée jusqu'ici)

Depuis PowerShell, sans mettre le jeton dans l'URL :

```powershell
$token = Read-Host 'ADMIN_API_TOKEN (copié depuis Render)' -AsSecureString
$plain = [System.Net.NetworkCredential]::new('', $token).Password
Invoke-RestMethod -Method Post -Uri 'https://VOTRE-SERVICE.onrender.com/api/diagnostic' -Headers @{ Authorization = "Bearer $plain" }
```

Résultat attendu : `status: ok`, `items_checked: 5`, `bodies_available: 5`, `auth: NTLM` (ou `Basic`),
`folders_visible` ≈ votre nombre de dossiers. Ce test est **en lecture seule** (aucune écriture Exchange).
Si vous obtenez `UnexpectedChallenge`, `UnsupportedAuth` ou `Unauthorized`, ne désactivez rien :
notez le code et signalez-le pour adapter le connecteur.

Passez ensuite `FIRESTORE_ENABLED=true`, redéployez et relancez le diagnostic : `store_selftest: ok`
confirme l'écriture, la lecture et le verrou dans Firestore.

### 6. Interface

Ouvrez `https://VOTRE-SERVICE.onrender.com`, connectez-vous avec l'utilisateur Firebase créé à
l'étape 2.4. Un autre compte est refusé (l'écran affiche alors son UID). Dans **Réglages** :
renseignez votre nom, votre signature, vos projets ; dans **Classement**, actualisez les dossiers.
Cliquez **Synchroniser** pour un premier passage.

Premier essai conseillé des écritures (sur un mail de test) : créer un brouillon depuis l'onglet
Brouillons et le vérifier dans Outlook PC, webmail et mobile ; déplacer un mail puis **Annuler**
dans le journal.

### 7. cron-job.org (une fois les tests manuels réussis)

1. <https://cron-job.org> → compte gratuit → **Create cronjob**.
2. URL : `https://VOTRE-SERVICE.onrender.com/internal/tick`.
3. Planification personnalisée : minutes `0,10,20,30,40,50`, heures `8-17`, jours lundi à vendredi,
   fuseau **Europe/Paris** (passages de 08:00 à 17:50).
4. **Advanced** → méthode **POST** ; en-tête `Authorization` = `Bearer VOTRE_CRON_SECRET`.
5. Le serveur vérifie aussi la plage horaire (réglable dans **Réglages**) : un appel hors plage
   ne touche pas à Exchange. Si vous changez la plage, modifiez aussi la planification cron-job.org.

**Réveil de Render** : le service gratuit s'endort après 15 min sans visite et met environ une
minute à redémarrer ; cron-job.org abandonne à 30 s. Le premier appel du matin peut donc être
signalé en échec par cron-job.org alors que le serveur a bien démarré : c'est attendu, le passage
suivant traite normalement. Fiez-vous à la « dernière synchro réussie » affichée dans l'application.

---

## Variables d'environnement

| Variable | Obligatoire | Rôle |
|---|---|---|
| `OVH_EMAIL`, `OVH_PASSWORD` | oui | Accès EWS (`https://ex.mail.ovh.net/EWS/Exchange.asmx`, NTLM puis Basic, HTTPS seulement) |
| `ADMIN_API_TOKEN` | oui | `POST /api/diagnostic` (≥ 32 caractères) |
| `CRON_SECRET` | oui | `POST /internal/tick` (≥ 32 caractères, distinct) |
| `FIRESTORE_ENABLED` | oui en production | `true` = données dans Firestore ; sinon mémoire (perdue au redémarrage) |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | oui | Identité serveur (Firestore + vérification des connexions) |
| `FIREBASE_WEB_CONFIG` | oui | `apiKey`, `authDomain`, `projectId` (publics) |
| `ALLOWED_UIDS` | oui | UID(s) Firebase autorisés, séparés par des virgules ; le premier est le propriétaire des données |
| `OPENROUTER_API_KEY` | pour l'IA | Sans clé : analyse par règles uniquement |
| `OPENROUTER_MODEL` | non | Modèles par ordre de préférence (défaut ci-dessus) |
| `OPENROUTER_NO_TRAINING` | non | `true` : exclut les fournisseurs qui conservent/entraînent |
| `OPENROUTER_REASONING` | non | `off` (défaut, plus rapide) ; un modèle qui l'impose est relancé en raisonnement minimal |
| `TICK_BUDGET_SECONDS` | non | Budget par passage (défaut 24) |
| `UI_TOKEN_LOGIN` | non | `true` = connexion à l'interface avec `ADMIN_API_TOKEN` (essais locaux uniquement) |
| `EWS_ENDPOINT` | non | Ne changer que sur indication d'OVH |

## Développement local

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python --require-hashes -r requirements.txt
```

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Mode démo (boîte simulée, mails fictifs, aucune connexion OVH ; vraie IA si `OPENROUTER_API_KEY` est défini) :

```bash
ADMIN_API_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") .venv/bin/python -m scripts.demo
```

Puis <http://127.0.0.1:8000>, connexion avec ce jeton. Les dépendances sont verrouillées avec
empreintes dans `requirements.txt` (généré depuis `requirements.in` par
`uv pip compile -p 3.12 requirements.in -o requirements.txt --generate-hashes`).

## Architecture

```
app.py                 point d'entrée WSGI (gunicorn, 1 worker, 4 threads)
assistant/ews.py       client SOAP EWS : liste blanche d'opérations, aucun envoi, SaveOnly
assistant/engine.py    synchro, analyse, tâches, brouillons, classement, verrou, purge
assistant/ai.py        règles locales + OpenRouter + validation stricte de la sortie
assistant/store.py     Firestore (prod) / mémoire (tests), données sous users/{uid}/
assistant/web.py       API JSON + sécurité (Firebase Auth + UID autorisé, CSP stricte)
assistant/static/      interface (HTML/CSS/JS sans dépendance externe)
diagnostics/           script PowerShell local d'origine (ne pas relancer machinalement)
```

Données Firestore (`users/{uid}/…`) : `messages`, `tasks`, `drafts`, `projects`, `settings`,
`sync` (état, quota IA), `sync_cursors`, `jobs` (verrou), `audit`, `meta` (arborescence).
Aucun corps de mail n'est stocké : le texte complet est relu dans Exchange à la demande
(bouton « Afficher le mail »). Sont conservés : objet, expéditeur/destinataires, résumé,
action, brouillon proposé. Purge automatique après 90 jours (réglable) ; tâches conservées.

## Sécurité

- Interface : jeton Firebase vérifié côté serveur **et** UID dans `ALLOWED_UIDS` ; requête anonyme → 401,
  autre compte → 403. Règles Firestore entièrement fermées (`firestore.rules`) : seul le serveur lit/écrit.
- Jetons techniques distincts et non interchangeables ; comparaison à temps constant ; refus si < 32 caractères.
- EWS : HTTPS uniquement, aucune redirection suivie, TLS vérifié, XML analysé avec `defusedxml`,
  opérations en liste blanche, `MessageDisposition="SaveOnly"` imposé, suppression limitée aux
  brouillons de l'assistant non modifiés (vers Éléments supprimés).
- Les réponses et journaux ne contiennent ni contenu de mail, ni XML, ni secret ; `/internal/tick`
  ne renvoie que des compteurs (cron-job.org peut les enregistrer).
- Interface : tout contenu de mail est inséré comme texte (jamais interprété en HTML), aucune image
  externe chargée, CSP `script-src 'self'`, pas de dépendance JavaScript externe.

## Points ouverts (à ne pas masquer)

- **Connexion EWS depuis Render non encore testée** (étape 5). Le code SOAP Python est couvert par
  des tests sur des réponses simulées, pas sur le serveur OVH réel. Opérations à valider en réel :
  `SyncFolderItems`, `ReplyToItem` en `SaveOnly`, propriété de corrélation, `MoveItem`.
- **Firestore non testé en réel** dans cet environnement : le diagnostic (`store_selftest`) le vérifie au déploiement.
- **Autorisation de l'entreprise** pour envoyer le contenu des mails à un fournisseur d'IA externe : non vérifiée.
- Quotas gratuits Render / Firestore / OpenRouter : susceptibles d'évoluer ; ce n'est pas un SLA.
- Une réponse envoyée depuis un autre appareil n'est détectée que si elle arrive dans Éléments envoyés de la boîte.
- Pièces jointes : seuls leurs noms sont transmis à l'IA (contenu non lu).
