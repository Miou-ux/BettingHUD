# Guide Obsidian — BettingHUD

Comment utiliser le coffre documentation du projet et **où écrire quoi**.

## Principe du dépôt

| Zone | Rôle | Outil principal |
|------|------|-----------------|
| `docs/` | **Documentation officielle** (architecture, changelog, UI, modèle) | **Obsidian** ou Cursor |
| `app/`, `scripts/`, `models/` | Code et pipelines | **Cursor** |
| `data/`, `models/*.pkl` | Données locales (souvent gitignorées) | Scripts / dashboard |

> **Règle** : toute décision, évolution ou spec durable du projet → **documenter dans `docs/`** (fichier existant ou nouvelle note liée depuis [[Home]]). Ne pas laisser la doc uniquement dans le chat Cursor ou des notes hors repo.

---

## Coffre Obsidian

| Élément | Valeur |
|---------|--------|
| Dossier racine | `O:\Miouppy\Documents\BettingHUD\docs` |
| Nom affiché Obsidian | **BettingHUDDOCS** (label uniquement) |
| Note d’accueil | [[Home]] |
| Config | `docs/.obsidian/` (workspace local partiellement gitignoré) |

**Ouvrir le coffre** : Obsidian → *Open folder as vault* → sélectionner le dossier **`docs`**, pas un sous-dossier.

L’ancien coffre `BettingHuD/` a été supprimé ; une seule source de vérité.

---

## Démarrage (chaque session)

1. Ouvrir le coffre **BettingHUDDOCS**.
2. Ouvrir **[[Home]]** (bookmark recommandé).
3. Naviguer via les liens `[[...]]` vers la note utile.

Raccourcis :

- `Ctrl + O` — ouvrir une note par nom
- `Ctrl + Shift + F` — recherche globale dans le coffre
- `Ctrl + G` — graphe des liens (après avoir créé des `[[liens]]`)

---

## Quelle note pour quel sujet ?

| Question | Note |
|----------|------|
| Quoi de neuf ? | [[CHANGELOG_RECENT]] |
| Architecture live, snapshot, v47 | [[ARCHITECTURE_ACTUELLE_ET_MISES]] |
| Proba, EV, Kelly, backtest | [[PREDICTION_ET_MISE]] |
| Simulations top 10 proba (2024–2026) | [[BACKTEST_TOP10_PROBA_SIMULATIONS]] |
| Thème UI dashboard | [[UI_THEME_QUANT]] |
| Onglet / chart top probas (+ toggle EV Live) | [[CHART_TOP_PROBAS_JOUR]] |
| Historique modèle v45 | [[MODELE_V45_CHANGELOG_ET_PERFORMANCE]] |
| Vue d’ensemble (legacy) | [[ARCHITECTURE]] |

Index : [[Home]].

---

## Workflow quotidien

```text
1. Code / run (Cursor ou terminal)
      rebuild, pipeline matin, dashboard Streamlit

2. Doc (Obsidian → docs/)
      mettre à jour CHANGELOG_RECENT si comportement ou config change
      mettre à jour ARCHITECTURE_* / UI_* / CHART_* si structure ou UI change

3. Git
      commit les .md de docs/ avec le code concerné
```

### Notes personnelles / journal

Créer des fichiers sous **`docs/notes/`** (ex. `docs/notes/2026-05-27.md`) pour :

- compte-rendu de session,
- idées non encore implémentées,
- liens vers `[[CHANGELOG_RECENT]]` quand une idée est livrée.

Ne pas dupliquer la doc technique à la racine de `docs/` : une note = un sujet clair, liée depuis le changelog ou Home si elle devient référence.

---

## Éditer la documentation (checklist)

Quand tu modifies le projet, mets à jour **au minimum** :

| Type de changement | Fichier(s) `docs/` |
|--------------------|---------------------|
| Feature ou correctif livré | [[CHANGELOG_RECENT]] (section datée) |
| Pipeline live, snapshot, env `BETTINGHUD_*` | [[ARCHITECTURE_ACTUELLE_ET_MISES]] + changelog |
| Déploiement VPS Ubuntu | [[DEPLOY_SERVEUR]] |
| PREPROD/PROD, sync données, incident serveur | [[ENVIRONNEMENTS]] + [[OPS_PROD_DEPANNAGE]] |
| ML, features, calibration, EV | [[PREDICTION_ET_MISE]] + changelog |
| UI Streamlit, thème, onglets | [[UI_THEME_QUANT]] + changelog |
| Nouveau graphique / onglet dédié | Nouvelle note `CHART_*.md` ou section existante + [[Home]] |
| Commande ou script nouveau | [[Home]] (bloc commandes) + changelog |

Format changelog : date, **pourquoi**, fichiers touchés, commandes de rebuild si besoin.

---

## Liens Obsidian

- Lien interne : `[[NOM_FICHIER]]` (sans `.md`)
- Lien vers section : `[[CHANGELOG_RECENT#0. Mise à jour …]]`
- Lien vers le README code : chemin relatif dans une note — `` [`README.md`](../README.md) ``

Après avoir créé une nouvelle note `.md` dans `docs/`, **ajouter un lien depuis [[Home]]** (ou depuis le changelog) pour qu’elle soit découvrable.

---

## Git

- Versionner les `.md` sous `docs/` (sauf état UI Obsidian volatile).
- Ignoré : `docs/.obsidian/workspace.json`, `workspace-mobile.json`, `plugins/`.
- Commit recommandé : code + doc dans le **même commit** quand la feature et sa doc vont ensemble.

Plugin communautaire **Obsidian Git** : optionnel ; un commit depuis Cursor suffit.

---

## Réglages Obsidian recommandés

**Settings → Files & links**

- Default location for new notes : `notes`
- New link format : Relative path to file

**Settings → Editor**

- Readable line length : on

**Core plugins**

- Backlinks, Outgoing links : on
- Bookmarks : épingle [[Home]]

---

## Erreurs courantes

1. Ouvrir un sous-dossier comme coffre → toujours **`docs`**.
2. Documenter seulement dans Cursor chat → recopier l’essentiel dans `docs/`.
3. Dupliquer `CHANGELOG_RECENT.md` hors de `docs/`.
4. Oublier de lier une nouvelle note depuis [[Home]].

---

## Voir aussi

- [[Home]] — index
- [`../README.md`](../README.md) — installation et commandes projet
