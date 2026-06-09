# Google Search Console — CourtAlpha

Checklist one-shot (~30 min) pour indexer les pages publiques SEO.

---

## 1. Ajouter la propriété

1. [Google Search Console](https://search.google.com/search-console)
2. **Ajouter une propriété** → `https://courtalpha.tech` (préfixe d’URL)
3. Vérification recommandée : enregistrement DNS **TXT** chez le registrar du domaine
   - Alternative : fichier HTML dans `frontend/public/` puis redeploy

---

## 2. Soumettre le sitemap

URL : `https://courtalpha.tech/sitemap.xml`

Le sitemap est généré au build React (`npm run build` → `dist/sitemap.xml`).

Pages indexables attendues :
- `/1-day-1-pick`
- `/pricing`
- `/methodologie`
- `/1-day-1-pick/archive`
- `/1-day-1-pick/archive/AAAA-MM` (6 derniers mois)

Search Console → **Sitemaps** → ajouter l’URL ci-dessus.

---

## 3. Contrôles post-soumission (J+3 à J+14)

| URL | Attendu |
|-----|---------|
| `/1-day-1-pick` | Indexée, requêtes « pronostic tennis » |
| `/methodo` | Indexée |
| `/pricing` | Indexée |

Inspection d’URL → **Demander une indexation** pour `/1-day-1-pick` en priorité.

---

## 4. Suivi

- Performance → requêtes contenant `tennis`, `pick`, `value bet`
- Couverture → corriger les erreurs 404
- Core Web Vitals (mobile)

Objectif réaliste 30 jours : 50–200 impressions/mois (niche tennis FR).
