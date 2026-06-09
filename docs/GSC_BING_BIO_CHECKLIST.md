# Checklist GSC + Bing + Bio X (one-shot)

Actions manuelles pour compléter l'acquisition organique.

---

## Google Search Console

1. [Google Search Console](https://search.google.com/search-console)
2. Propriété : `https://courtalpha.tech`
3. Vérification : enregistrement DNS TXT (recommandé)
4. Sitemaps → ajouter : `https://courtalpha.tech/sitemap.xml`
5. Inspection URL → demander indexation de `/1-day-1-pick` et `/methodo`

---

## Bing Webmaster Tools

1. [Bing Webmaster](https://www.bing.com/webmasters)
2. Ajouter `courtalpha.tech`
3. Vérifier via DNS ou fichier (IndexNow key déjà déployée : `ca8f3e2b1d4c49a7b6e5f0d9c8b7a6.txt`)
4. Soumettre sitemap : `https://courtalpha.tech/sitemap.xml`

IndexNow ping automatique à chaque deploy frontend (`npm run indexnow`).

---

## Bio X (CourtAlphaX)

Lien fixe dans la bio du compte X :

```
https://courtalpha.tech/1-day-1-pick?utm_source=twitter&utm_medium=bio&utm_campaign=bio
```

Texte bio suggéré :
```
🎾 Value bets tennis ATP/WTA · track record public auditable
1 pick/jour · probas modèle · courtalpha.tech
```

---

## Baseline trafic (semaine 0)

Noter dans un doc perso avant lancement acquisition :

- Sessions 7 j (admin Fréquentation)
- Top 3 pages
- Top 3 sources UTM

Comparer avec rapport Telegram hebdo (`traffic_weekly_report.py`).
