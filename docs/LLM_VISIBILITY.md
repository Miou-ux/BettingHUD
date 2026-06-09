# Visibilité LLM — CourtAlpha

Comment améliorer la découvrabilité de CourtAlpha dans ChatGPT, Perplexity, Gemini, Copilot, etc.

> Voir aussi : [[GOOGLE_SEARCH_CONSOLE]] · [[TELEGRAM_CHANNEL_ACQUISITION]]

---

## Fichiers déployés

| Fichier | URL | Rôle |
|---------|-----|------|
| `llms.txt` | https://courtalpha.tech/llms.txt | Résumé produit + liens (standard crawlers IA) |
| `llms-full.txt` | https://courtalpha.tech/llms-full.txt | Version étendue |
| `{INDEXNOW_KEY}.txt` | https://courtalpha.tech/ca8f3e2b1d4c49a7b6e5f0d9c8b7a6.txt | Clé IndexNow (Bing/Yandex) |
| `/methodo` | Bloc « Qu'est-ce que CourtAlpha ? » | Contenu factuel citable |
| JSON-LD | `SoftwareApplication` + `Organization` | Schema.org sur pages publiques |

Génération au build : `npm run build` → scripts `generate-llms-txt.ts`.

---

## IndexNow

Après chaque déploiement frontend (`deploy_frontend.ps1`), ping automatique :

```bash
npm run indexnow
```

Désactiver : `INDEXNOW_DISABLED=1`

Portail Bing : [Bing Webmaster Tools](https://www.bing.com/webmasters) — ajouter `courtalpha.tech` et vérifier la propriété.

---

## Checklist manuelle

1. Google Search Console — sitemap soumis
2. Bing Webmaster Tools — même sitemap + vérif IndexNow
3. Tester dans Perplexity : *« CourtAlpha tennis value bet »*
4. Mentions externes avec formulation fixe : **CourtAlpha**, **courtalpha.tech**, **1 Day 1 Pick**

---

## Scripts

```bash
cd CourtAlpha/frontend
npm run build          # inclut llms.txt dans dist/
npm run indexnow       # ping après deploy
```
