# Prompt: update ClickSense from an existing knowledge base

A ready-to-use prompt for an AI coding agent (Claude Code or similar) that has access to this
repository: it reads a folder of Markdown files describing your data and updates the whole
semantic layer from it (dictionary, metrics, glossary, examples, knowledge documents, skills, and
the settings the knowledge base justifies), then validates everything against ClickHouse and opens
a pull request for review.

**How to use it**

1. Make sure the local ClickHouse (or your real one, via `.env`) is reachable:
   `.venv/bin/clicksense --check`.
2. Copy the prompt below, replace `<CHEMIN_CLICKSENSE>` and `<CHEMIN_KB>`, adjust the content
   language if needed, and give it to the agent.
3. Review the pull request it opens: inventory, what was not integrated and why, conflicts to
   arbitrate. It is told not to merge it.

The prompt only relies on what exists in this repository: the `clicksense-admin` commands, the
allowed keys listed at the top of `src/clicksense/knowledge.py`, the templates in
`src/clicksense/templates/` and the recipes in [UPDATING.md](UPDATING.md). If you rename a command
or a key, update this prompt too.

The prompt is in French (the knowledge bases it was written for are); the content it produces is in
English, like the rest of the semantic layer.

---

```text
Tu vas mettre à jour intégralement la couche sémantique de ClickSense (serveur MCP ClickHouse) à partir
d'une base de connaissances existante.

- Projet ClickSense : <CHEMIN_CLICKSENSE>            (ex. ~/CLAUDE/clicksense)
- Base de connaissances source : <CHEMIN_KB>          (répertoire de fichiers .md, sous-dossiers inclus)
- Langue du contenu à écrire : anglais (garde les mots des utilisateurs dans leur langue comme synonymes)

Toutes les commandes se lancent depuis <CHEMIN_CLICKSENSE> ; sous Windows, remplace
`.venv/bin/clicksense-admin` par `admin-windows.bat` et `.venv/bin/clicksense` par `check-windows.bat`.

## 0. Avant de commencer
1. Lis, dans le projet : docs/UPDATING.md (guide pas à pas), semantic/README.md, les modèles commentés de
   src/clicksense/templates/, les listes de clés autorisées en tête de src/clicksense/knowledge.py
   (TABLE_KEYS, COLUMN_KEYS, JOIN_KEYS, DIMENSION_KEYS, METRIC_KEYS, TERM_KEYS, SENSE_KEYS, EXAMPLE_KEYS,
   DOC_KEYS, SKILL_KEYS), et le contenu actuel de semantic/ (c'est la référence de style).
2. Vérifie que ClickHouse répond : `.venv/bin/clicksense --check`. S'il ne répond pas, arrête-toi et
   dis-le : la validation live est obligatoire.
3. Crée une branche git `kb/import-<date>` ; ne travaille jamais directement sur main.

## 1. Inventaire (ne rien écrire encore)
Lis TOUS les fichiers .md de <CHEMIN_KB>. Pour chacun, classe chaque information dans une de ces cibles :

| Information trouvée | Cible ClickSense |
|---|---|
| Description d'une table, grain, propriétaire, fraîcheur, pièges | semantic/dictionary/<base>.yaml (table) |
| Sens d'une colonne, unité, donnée personnelle, valeurs codées et leur signification, mots des utilisateurs | dictionary (colonne : description, unit, pii, values, value_synonyms) |
| Relations entre tables | dictionary (joins, dimensions) |
| Définition d'un indicateur (formule, filtres, période, axes d'analyse) | semantic/metrics.yaml |
| Vocabulaire métier, abréviations, mots à plusieurs sens | semantic/glossary.yaml (senses si ambigu) |
| Requêtes SQL validées avec la question qu'elles résolvent | semantic/examples.yaml |
| Règles de gestion, conventions, calendrier, exceptions | semantic/knowledge/<sujet>.md |
| Procédures d'analyse récurrentes (étapes à suivre) | semantic/skills/<nom>/SKILL.md |
| Paramètres techniques (bases autorisées, tables à masquer, limites) | clicksense.yaml (voir §4) |

Produis un tableau d'inventaire : fichier source → informations → cible. Liste aussi les informations
introuvables dans ClickHouse, contradictoires entre fichiers, ou trop vagues pour être utilisées.

## 2. Confronter au schéma réel
- Pour chaque table citée, vérifie qu'elle existe et ce qui est déjà documenté :
  `.venv/bin/clicksense-admin kb search "<nom ou sujet de la table>"` et
  `.venv/bin/clicksense-admin kb bootstrap <base.table> --print-only` (colonnes réelles, sans rien écrire).
- Table pas encore documentée : `.venv/bin/clicksense-admin kb bootstrap <base.table>` d'abord (colonnes,
  commentaires, valeurs stockées), PUIS enrichis-la avec la base de connaissances.
- Pour chaque valeur métier citée (pays, statut, canal…), vérifie la valeur réellement stockée :
  `.venv/bin/clicksense-admin kb resolve <base.table> <colonne> <mots>`. Le dictionnaire mappe valeur
  stockée → libellé (`values`) et valeur stockée → mots des utilisateurs (`value_synonyms`) ; jamais
  l'inverse.

## 3. Écrire, en respectant ces règles
- Utilise `.venv/bin/clicksense-admin kb new <metric|term|example|knowledge|skill> <nom>` pour créer chaque
  nouvelle entrée, puis remplace tous les TODO. N'utilise que les clés autorisées.
- N'invente rien : chaque description, formule, valeur ou règle doit venir de la base de connaissances
  ou du schéma ClickHouse. Si une information manque, laisse l'entrée de côté et signale-la.
- Ne supprime ni ne réécris une entrée existante sans le signaler. En cas de conflit entre la base de
  connaissances et l'existant, garde l'existant et liste le conflit pour arbitrage.
- Métriques : `expression` est un agrégat qui utilise l'alias de la table défini dans le dictionnaire ;
  les conditions toujours appliquées vont dans `filters` ; un mot ne doit désigner qu'UNE métrique.
  Un mot qui désigne plusieurs choses va dans le glossaire avec plusieurs `senses` (l'agent posera alors
  la question à l'utilisateur).
- Exemples SQL : uniquement des requêtes présentes dans la base de connaissances (ou réécrites fidèlement),
  toujours filtrées sur la colonne de temps.
- Documents de connaissance : un sujet par fichier, paragraphes courts et autonomes (l'agent reçoit le
  paragraphe le plus pertinent), `applies_to` renseigné avec les tables concernées.
- Skills : `name` identique au nom du dossier, `description` précise (une phrase), `when_to_use` avec les
  mots que les utilisateurs emploient réellement (plusieurs langues possibles), étapes numérotées citant
  les outils ClickSense (get_context, query_metric, resolve_values, validate_query, run_query,
  ask_user_to_clarify…).
- Données personnelles : marque `pii: true` sur toute colonne identifiante (nom, e-mail, téléphone,
  adresse, identifiants personnels).
- Garde le YAML valide : mets entre guillemets toute valeur contenant `:`, `,`, `#` ou commençant par
  un caractère spécial.

## 4. Paramètres (clicksense.yaml)
- Consulte `.venv/bin/clicksense-admin config --example` (tous les paramètres documentés) et
  `.venv/bin/clicksense-admin config --show` (valeurs actuelles).
- Ne modifie un paramètre que si la base de connaissances le justifie explicitement (bases autorisées,
  tables à masquer, limites de requête…). Indique la source de chaque changement.
- Ne mets JAMAIS de secret (mot de passe, clé, jeton) dans un fichier : ils vont dans .env, que tu ne
  modifies pas. Signale ceux qui manquent.
- Si une limite doit dépasser le profil du compte ClickHouse, ne la change pas : signale qu'il faut d'abord
  modifier le profil (dev/production-account.sql).

## 5. Valider, puis tester
1. `.venv/bin/clicksense-admin kb validate --live` → corrige jusqu'à 0 erreur et 0 avertissement.
2. Pour 10 à 15 questions réalistes tirées de la base de connaissances :
   `.venv/bin/clicksense-admin kb search "<question>"`. Vérifie que la bonne table, la bonne métrique et
   les bonnes valeurs ressortent, et que les mots ambigus apparaissent sous « Ambiguities ». Ajuste
   synonymes et descriptions sinon.
3. Pour les nouvelles métriques, compare au moins une valeur calculée à un chiffre de référence de la base
   de connaissances, s'il en existe un : via `.venv/bin/clicksense-chat -q "..."` si un LLM est configuré
   dans .env, sinon avec une requête SQL équivalente lancée par un client MCP (validate_query puis
   run_query).
4. `.venv/bin/python -m pytest -q` doit rester vert.

## 6. Livrer
- Commit sur la branche, puis une pull request dont la description contient :
  - le tableau d'inventaire (source → cible) ;
  - ce qui a été ajouté / modifié, par fichier ;
  - les informations NON intégrées et pourquoi (manquante, contradictoire, absente de ClickHouse) ;
  - les conflits avec l'existant à arbitrer ;
  - les résultats de kb validate --live et des recherches de test.
- Ne fusionne pas la PR : je la relirai.

Travaille par lots (une base ou un domaine à la fois), en lançant kb validate après chaque lot.
Si quelque chose bloque ou est ambigu, demande-moi plutôt que de supposer.
```
