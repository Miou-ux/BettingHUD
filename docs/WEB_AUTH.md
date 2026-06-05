# Authentification web — BettingHUD

Connexion au dashboard Streamlit par **compte local** (fichier JSON, hors git).

> Voir aussi : [[TELEGRAM_TOP5]] (bankroll Telegram liée) · [[ENVIRONNEMENTS]] · [[DEPLOY_SERVEUR]]

---

## 1. Fichiers

| Fichier | Rôle |
|---------|------|
| `scripts/web_auth.py` | Login, session Streamlit, reset mot de passe |
| `scripts/web_email.py` | Envoi SMTP (lien reset) |
| `scripts/web_password_reset.py` | Jetons reset (TTL 1 h) |
| `scripts/init_web_user.py` | CLI création / mise à jour utilisateur |
| `data/web_users.json` | Comptes (hash PBKDF2, **gitignored**) |
| `data/web_avatars/` | Photos de profil web (`{username}.jpg/png/webp`, **gitignored** via `data/`) |
| `data/web_password_reset_tokens.json` | Jetons actifs (**gitignored**) |
| `app/dashboard.py` | Gate `require_web_login()` en tête d’app |

---

## 2. Compte Miouppy

| Champ | Valeur |
|-------|--------|
| Utilisateur | `miouppy` |
| Rôle | `owner` |
| Telegram lié | `7113749284` (paris dashboard → même BR que `/br`) |

### Rôles

| Rôle | Accès |
|------|--------|
| `owner` | Compte principal — accès admin total (miouppy) |
| `admin` | Même accès admin que owner |
| `user` | Live, paris, portfolio, profil (CourtAlpha) |

Les rôles **`owner`** et **`admin`** débloquent backtest, tracking modèle et statut système (`is_admin` dans `web_auth.py`). Le username **`miouppy`** est toujours admin même sans rôle explicite.

Création / mise à jour :

```bash
py -3 scripts/init_web_user.py \
  --username miouppy --display-name Miouppy --role owner \
  --password "…" --email miouppy86@gmail.com \
  --telegram-user-id 7113749284
```

Sur PROD : ne pas committer le mot de passe ; utiliser la CLI sur le serveur ou `BETTINGHUD_WEB_PASSWORD_MIOUPPY` dans `.env` (sync au démarrage).

---

## 3. Connexion

1. Ouvrir le dashboard → formulaire **Utilisateur** / **Mot de passe**
2. Session stockée dans `st.session_state["web_auth_user"]`
3. Bandeau compte + **Paramètres → Compte** (déconnexion, changement MDP)

Les nouveaux paris Kelly depuis le dashboard enregistrent `telegram_user_id` si l’utilisateur connecté en a un (alignement BR Telegram).

### 3.1 CourtAlpha (React)

L’app **CourtAlpha** réutilise les mêmes comptes (`web_users.json`) via l’API FastAPI :

| Champ profil | Stockage |
|--------------|----------|
| Nom affiché | `display_name` |
| Photo | `data/web_avatars/{username}.{ext}` → `GET /api/auth/avatar/{username}` |
| Telegram | `telegram_user_id`, `telegram_username` (optionnel, affiché côté UI) |

Page React `/profile` ; `BETTINGHUD_WEB_AUTH_REQUIRED=1` sur PROD.

**Accès CourtAlpha (PROD)** : visiteurs → Live Tracker, Paris du jour, Top 5 uniquement. Portfolio, top-probas, backtest, etc. → login. Bankroll toujours liée au `telegram_user_id` du compte connecté.

**Streamlit legacy** : `https://admin.courtalpha.tech/` (ex-`:8502`). Variable `BETTINGHUD_WEB_BASE_URL` → même URL pour les liens reset mot de passe.

---

## 4. Mot de passe oublié

### 4.1 Flux

1. Page login → **Mot de passe oublié ?**
2. Saisie de l’e-mail → envoi d’un lien `/?reset_token=…` (valable **1 heure**)
3. Formulaire **nouveau mot de passe** (ou **Paramètres → Réinitialisation par e-mail** une fois connecté)

### 4.2 SMTP (`.env`)

```env
```env
BETTINGHUD_WEB_BASE_URL=https://admin.courtalpha.tech
```
BETTINGHUD_SMTP_HOST=smtp.gmail.com
BETTINGHUD_SMTP_PORT=587
BETTINGHUD_SMTP_USER=miouppy86@gmail.com
BETTINGHUD_SMTP_PASSWORD=mot_de_passe_application
BETTINGHUD_SMTP_FROM=miouppy86@gmail.com
```

Gmail : [mot de passe d’application](https://myaccount.google.com/apppasswords).

Sans SMTP, le reset par mail est indisponible ; la connexion classique reste active.

### 4.3 Variables optionnelles

| Variable | Défaut | Usage |
|----------|--------|--------|
| `BETTINGHUD_WEB_USERS_FILE` | `data/web_users.json` | Chemin comptes |
| `BETTINGHUD_WEB_RESET_TOKENS_FILE` | `data/web_password_reset_tokens.json` | Jetons reset |
| `BETTINGHUD_WEB_PASSWORD_MIOUPPY` | — | Sync auto compte miouppy au boot |
| `BETTINGHUD_WEB_EMAIL_MIOUPPY` | — | E-mail sync env (optionnel) |
| `BETTINGHUD_TELEGRAM_USER_ID_MIOUPPY` | `7113749284` | Lien Telegram sync env |

---

## 5. Sécurité

- Mots de passe : **PBKDF2-SHA256** (390k itérations), sel aléatoire
- Reset : message générique si e-mail inconnu (pas d’énumération de comptes)
- Fichiers sensibles dans `.gitignore`

---

## 6. PROD

Déployé sur **`bettinghud`** (`/opt/bettinghud`) — IP publique **http://192.95.30.217**

```bash
# Compte web (une fois)
cd /opt/bettinghud
./venv/bin/python scripts/init_web_user.py \
  --username miouppy --display-name Miouppy --role owner \
  --password '…' --email miouppy86@gmail.com \
  --telegram-user-id 7113749284

# SMTP Gmail (mot de passe d'application Google, pas le MDP du compte)
BETTINGHUD_SMTP_PASSWORD='xxxx xxxx xxxx xxxx' ./venv/bin/python scripts/patch_env_smtp.py \
  --base-url http://192.95.30.217 --smtp-password "$BETTINGHUD_SMTP_PASSWORD"

./venv/bin/python scripts/test_smtp_send.py
sudo systemctl restart bettinghud-dashboard
```

Depuis Windows (scp des scripts puis ssh) : voir `docs/DEPLOY_SERVEUR.md` § 8.
