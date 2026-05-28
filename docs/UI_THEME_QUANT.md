# Charte graphique — Terminal quant (BettingHUD)

Thème sombre institutionnel pour le dashboard Streamlit (`app/dashboard.py`), inspiré Bloomberg / TradingView / DeBank.

## Injection CSS

Fonction : **`_inject_quant_terminal_theme()`** — appelée juste après `st.set_page_config` (via **`st.html`**, pas `st.markdown`, pour que le `<style>` soit bien appliqué sous Streamlit 1.57+).

Polices chargées via Google Fonts : **Inter** (UI), **JetBrains Mono** (chiffres).

## Palette

| Token | Hex | Usage |
|-------|-----|--------|
| `--bg` | `#0B0C10` | Fond global |
| `--bg-elevated` | `#12131A` | Sidebar, inputs |
| `--panel` | `#1C1D24` | Cartes, conteneurs |
| `--border` | `#2D3139` | Bordures 1 px |
| `--text` | `#FFFFFF` | Texte principal |
| `--muted` | `#8A8D98` | Texte secondaire |
| `--accent` | `#00B0FF` | Métriques, info UI |
| `--success` | `#00E676` | Value positive, premium |
| `--danger` | `#FF3838` | Risque / EV négatif |
| `--warning` | `#FFD600` | Alertes données, cotes book chart |

## Typographie quant

Classe **`.quant-num`** : police mono + `tabular-nums` pour alignement vertical des chiffres.

Appliquée automatiquement à :

- `.odd-highlight`, `.ev-highlight`, `.ev-highlight-neg`
- `st.metric` (valeurs)
- cellules `st.dataframe` / `st.table`
- inputs numériques

## Cartes Value Bet

- Conteneur : `st.container(border=True)` — fond `--panel`, **sans box-shadow**.
- **Premium** (Brier segment &lt; 0,18) : marqueur `.vb-card-premium-marker` + sélecteur CSS `:has()` → liseré vert `#00E676`.
- Meta-ligne : `.vb-card-meta` avec badges circuit + segment + Brier.

### Badges

| Classe | Rôle |
|--------|------|
| `.badge-circuit-atp` / `-wta` | Circuit ATP / WTA |
| `.badge-segment-premium` / `-std` | Clé segment `[WTA_Clay_G]` |
| `.brier-badge-premium` / `-std` | Score Brier segment |

Helpers Python : `_circuit_badge_html()`, `_segment_chip_badge_html()`, `_segment_brier_badge_html()`.

## Boutons

- **Secondaire** (défaut) : fond `--bg-elevated`, bordure `--border`, radius **4 px**.
- **Parier** : `st.button(..., type="primary")` — fond vert foncé `#0a3d22`, hover `#00E676` (plus de rouge Streamlit : `.streamlit/config.toml` + CSS sliders/radios).

## Panneau EV (cartes value bet / in-play)

Helper **`_ev_comparison_panel_html()`** — trois colonnes :

| Colonne | Contenu |
|---------|---------|
| Bookmaker | cote book, proba implicite, **EV book** |
| Juste (modèle) | cote fair, proba modèle, **écart pp** vs book |
| Votre cote | cote saisie, EV à cette cote |

Verdict en tête : « Value forte / modérée / marge faible / pas de value » (seuils 15 % / 5 % EV book).

## Tableaux

- Padding cellules réduit (4×8 px).
- Police mono sur les cellules numériques.
- Pas de scroll horizontal forcé côté CSS (overflow auto si nécessaire).

## Fichiers liés

- Graphique top probas : `docs/CHART_TOP_PROBAS_JOUR.md`
- Changelog : `docs/CHANGELOG_RECENT.md` § UI

## Évolutions

- Thème clair optionnel (`BETTINGHUD_THEME=light`).
- Variables d’environnement pour accent / success.
