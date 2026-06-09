# Facturation ETH — CourtAlpha

Module premium self-hosted (Base L2). Phase 0 : gating + grant manuel. Phase 1+ : dépôt ETH sur adresse HD unique par commande.

## Tiers d'accès

| Tier | Pages |
|------|--------|
| Public | `/1-day-1-pick`, `/pricing` |
| Gratuit (compte) | Portfolio, profil |
| Premium | Live, Paris du jour, Top 5, Top probas |
| Admin | backtest, tracking, fréquentation, settings |

## Phase 0 — grant manuel

```bash
cd /opt/bettinghud
./venv/bin/python scripts/grant_premium.py --username USER --days 30
```

Variables :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `COURTALPHA_BILLING_ENABLED` | `1` | `0` = désactive les garde-fous premium |
| `COURTALPHA_BILLING_PRICE_WEI` | `500000000000000` | Prix plan seed (~0.0005 ETH) |
| `COURTALPHA_BILLING_CHAIN_ID` | `8453` | Base mainnet |

Tables SQLite (`bettinghud.db`) : `billing_plans`, `billing_orders`, `web_user_entitlements`.

API : `GET /api/billing/plans`, `GET /api/billing/me` (auth).

## Phase 1–2 — Paiement ETH (adresses HD)

| Composant | Fichier |
|-----------|---------|
| HD wallet | `scripts/billing_hd.py` (BIP-44 `m/44'/60'/0'/0/{index}`) |
| Indexer | `scripts/billing_indexer.py` (cron `deploy/cron/billing-indexer`) |
| API | `POST /api/billing/orders`, `GET /api/billing/orders/{id}`, `GET /api/billing/config` |
| UI | `PremiumCheckout` sur `/pricing` (MetaMask + viem) |

Variables serveur :

| Variable | Requis | Rôle |
|----------|--------|------|
| `COURTALPHA_BILLING_MNEMONIC` | oui (mode HD) | Seed BIP-39 — **secret, hors git** |
| `COURTALPHA_BILLING_RPC_URL` | oui | RPC Base (ex. `https://mainnet.base.org`) |
| `COURTALPHA_BILLING_CHAIN_ID` | non | `8453` mainnet, `84532` Sepolia |
| `COURTALPHA_BILLING_PRICE_WEI` | non | Prix plan par défaut |

Compteur d'index HD : clé `billing_hd_next_index` dans `bets_meta`.

Flux :

1. Utilisateur connecté → crée commande → reçoit `deposit_address` + `price_wei`
2. Envoie le montant exact en ETH à cette adresse (MetaMask ou transfert manuel)
3. Indexer poll `eth_getBalance` sur les commandes pending → crédite le premium
4. UI poll la commande jusqu'à `status=paid`

Retrait des fonds : dériver les clés depuis la mnemonic (index connu via `billing_orders.address_index`) et transférer vers un cold wallet.

## Legacy — contrat CourtAlphaPay (optionnel)

Si `COURTALPHA_BILLING_CONTRACT` est défini **sans** mnemonic HD, l'indexer peut aussi indexer les événements `Paid`. Voir `CourtAlpha/contracts/README.md`.
