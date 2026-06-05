#!/usr/bin/env python3
"""Authentification web Streamlit (utilisateurs locaux, fichier JSON)."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

import streamlit as st

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_USERS_PATH = _ROOT / "data" / "web_users.json"
_PBKDF2_ITERATIONS = 390_000
ADMIN_ROLES = frozenset({"owner", "admin"})


def is_admin(user: dict[str, Any] | None) -> bool:
    """True si rôle owner/admin ou compte miouppy (super-admin legacy)."""
    if not user or not user.get("username"):
        return False
    role = str(user.get("role") or "user").strip().lower()
    if role in ADMIN_ROLES:
        return True
    return str(user.get("username") or "").strip().lower() == "miouppy"


def _users_path() -> Path:
    raw = (os.getenv("BETTINGHUD_WEB_USERS_FILE") or "").strip()
    return Path(raw) if raw else _DEFAULT_USERS_PATH


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, iters_s, salt_hex, digest_hex = str(stored_hash or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return secrets.compare_digest(got, expected)


def _load_users_doc() -> dict[str, Any]:
    path = _users_path()
    if not path.is_file():
        return {"users": []}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("users"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"users": []}


def _save_users_doc(doc: dict[str, Any]) -> None:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def list_web_users() -> list[dict[str, Any]]:
    return list(_load_users_doc().get("users") or [])


def upsert_web_user(
    username: str,
    password: str,
    *,
    display_name: str | None = None,
    role: str = "user",
    telegram_user_id: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    uname = str(username or "").strip().lower()
    if not uname:
        raise ValueError("username requis")
    if not password:
        raise ValueError("password requis")
    doc = _load_users_doc()
    users: list[dict] = list(doc.get("users") or [])
    row: dict[str, Any] = {
        "username": uname,
        "display_name": (display_name or uname).strip(),
        "role": str(role or "user").strip() or "user",
        "password_hash": hash_password(password),
    }
    if telegram_user_id:
        row["telegram_user_id"] = str(telegram_user_id).strip()
    if email:
        row["email"] = str(email).strip().lower()
    replaced = False
    for i, u in enumerate(users):
        if str(u.get("username") or "").lower() == uname:
            merged = {**u, **row}
            if not email and u.get("email"):
                merged["email"] = u["email"]
            users[i] = merged
            replaced = True
            break
    if not replaced:
        users.append(row)
    doc["users"] = users
    _save_users_doc(doc)
    return row


def set_user_password(username: str, new_password: str) -> bool:
    uname = str(username or "").strip().lower()
    if not uname or not new_password:
        return False
    doc = _load_users_doc()
    users: list[dict] = list(doc.get("users") or [])
    found = False
    for i, u in enumerate(users):
        if str(u.get("username") or "").lower() == uname:
            users[i] = {**u, "password_hash": hash_password(new_password)}
            found = True
            break
    if not found:
        return False
    doc["users"] = users
    _save_users_doc(doc)
    return True


def set_user_email(username: str, email: str) -> bool:
    uname = str(username or "").strip().lower()
    addr = str(email or "").strip().lower()
    if not uname or not addr or "@" not in addr:
        return False
    doc = _load_users_doc()
    users: list[dict] = list(doc.get("users") or [])
    found = False
    for i, u in enumerate(users):
        if str(u.get("username") or "").lower() == uname:
            users[i] = {**u, "email": addr}
            found = True
            break
    if not found:
        return False
    doc["users"] = users
    _save_users_doc(doc)
    return True


def sync_users_from_env() -> None:
    """Crée/met à jour Miouppy si BETTINGHUD_WEB_PASSWORD_MIOUPPY est défini."""
    pwd = (os.getenv("BETTINGHUD_WEB_PASSWORD_MIOUPPY") or "").strip()
    if not pwd:
        return
    tg = (os.getenv("BETTINGHUD_TELEGRAM_USER_ID_MIOUPPY") or "7113749284").strip()
    email = (os.getenv("BETTINGHUD_WEB_EMAIL_MIOUPPY") or "").strip() or None
    upsert_web_user(
        "miouppy",
        pwd,
        display_name="Miouppy",
        role="owner",
        telegram_user_id=tg or None,
        email=email,
    )


def _find_user_record(username: str) -> dict[str, Any] | None:
    uname = str(username or "").strip().lower()
    for u in list_web_users():
        if str(u.get("username") or "").lower() == uname:
            return dict(u)
    return None


def _find_user_by_email(email: str) -> dict[str, Any] | None:
    addr = str(email or "").strip().lower()
    if not addr:
        return None
    for u in list_web_users():
        if str(u.get("email") or "").strip().lower() == addr:
            return dict(u)
    return None


def registration_enabled() -> bool:
    raw = (os.getenv("BETTINGHUD_WEB_REGISTRATION_OPEN") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def register_web_user(
    username: str,
    password: str,
    email: str,
    *,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Inscription publique — rôle user, e-mail obligatoire et unique."""
    if not registration_enabled():
        raise ValueError("Les inscriptions sont fermées.")

    uname = str(username or "").strip().lower()
    if not uname or len(uname) < 3 or len(uname) > 32:
        raise ValueError("Identifiant : 3 à 32 caractères.")
    if not all(c.isalnum() or c == "_" for c in uname):
        raise ValueError("Identifiant : lettres minuscules, chiffres et _ uniquement.")

    addr = str(email or "").strip().lower()
    if not addr or "@" not in addr or len(addr) > 254:
        raise ValueError("E-mail obligatoire et valide.")

    pwd = str(password or "")
    if len(pwd) < 4:
        raise ValueError("Mot de passe : 4 caractères minimum.")

    if _find_user_record(uname):
        raise ValueError("Cet identifiant est déjà utilisé.")
    if _find_user_by_email(addr):
        raise ValueError("Cet e-mail est déjà associé à un compte.")

    row = upsert_web_user(
        uname,
        pwd,
        display_name=(display_name or uname).strip()[:80],
        role="user",
        email=addr,
    )
    return _session_payload(row)


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    sync_users_from_env()
    rec = _find_user_record(username)
    if not rec:
        return None
    pwd = str(password or "")
    if not verify_password(pwd, str(rec.get("password_hash") or "")):
        # Tolérance espaces accidentels en bordure uniquement
        pwd_stripped = pwd.strip()
        if pwd_stripped != pwd and verify_password(pwd_stripped, str(rec.get("password_hash") or "")):
            return _session_payload(rec)
        return None
    return _session_payload(rec)


def _avatars_dir() -> Path:
    raw = (os.getenv("BETTINGHUD_WEB_AVATARS_DIR") or "").strip()
    return Path(raw) if raw else _ROOT / "data" / "web_avatars"


def _avatar_file_for_username(username: str) -> Path | None:
    uname = str(username or "").strip().lower()
    if not uname:
        return None
    base = _avatars_dir()
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = base / f"{uname}{ext}"
        if p.is_file():
            return p
    return None


def avatar_api_path(username: str) -> str | None:
    if _avatar_file_for_username(username):
        return f"/api/auth/avatar/{str(username).strip().lower()}"
    return None


def save_user_avatar(username: str, content: bytes, *, content_type: str | None = None) -> Path:
    uname = str(username or "").strip().lower()
    if not uname:
        raise ValueError("username requis")
    if not content or len(content) > 2_500_000:
        raise ValueError("image invalide ou trop volumineuse (max 2.5 Mo)")
    ct = str(content_type or "").lower()
    ext = ".jpg"
    if "png" in ct:
        ext = ".png"
    elif "webp" in ct:
        ext = ".webp"
    dest_dir = _avatars_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob(f"{uname}.*"):
        if old.is_file():
            old.unlink()
    dest = dest_dir / f"{uname}{ext}"
    dest.write_bytes(content)
    return dest


def update_user_profile(
    username: str,
    *,
    display_name: str | None = None,
    telegram_user_id: str | None = None,
    telegram_username: str | None = None,
    clear_telegram: bool = False,
) -> dict[str, Any] | None:
    uname = str(username or "").strip().lower()
    if not uname:
        return None
    doc = _load_users_doc()
    users: list[dict] = list(doc.get("users") or [])
    found = False
    for i, u in enumerate(users):
        if str(u.get("username") or "").lower() != uname:
            continue
        row = dict(u)
        if display_name is not None:
            dn = str(display_name).strip()
            if dn:
                row["display_name"] = dn
        if clear_telegram:
            row.pop("telegram_user_id", None)
            row.pop("telegram_username", None)
        else:
            if telegram_user_id is not None:
                tg = str(telegram_user_id).strip()
                if tg:
                    row["telegram_user_id"] = tg
                else:
                    row.pop("telegram_user_id", None)
            if telegram_username is not None:
                tu = str(telegram_username).strip().lstrip("@")
                if tu:
                    row["telegram_username"] = tu
                else:
                    row.pop("telegram_username", None)
        users[i] = row
        found = True
        break
    if not found:
        return None
    doc["users"] = users
    _save_users_doc(doc)
    return _session_payload(users[i])


def _session_payload(rec: dict[str, Any]) -> dict[str, Any]:
    uname = str(rec.get("username") or "").lower()
    out = {
        "username": uname,
        "display_name": str(rec.get("display_name") or rec.get("username") or ""),
        "role": str(rec.get("role") or "user"),
        "telegram_user_id": rec.get("telegram_user_id"),
        "telegram_username": rec.get("telegram_username"),
        "email": rec.get("email"),
    }
    av = avatar_api_path(uname)
    if av:
        out["avatar_url"] = av
    return out


def request_password_reset(email: str, *, reset_path: str | None = None) -> tuple[bool, str]:
    """
    Envoie un e-mail de reset si le compte existe et SMTP est configuré.
    Retourne (smtp_ok, message_utilisateur) — message générique côté UI si compte inconnu.
    """
    from scripts.web_email import send_password_reset_email, smtp_configured, web_base_url
    from scripts.web_password_reset import create_reset_token

    addr = str(email or "").strip().lower()
    if not addr or "@" not in addr:
        return False, "Adresse e-mail invalide."

    if not smtp_configured():
        return False, (
            "Envoi d'e-mail non configuré sur le serveur "
            "(BETTINGHUD_SMTP_HOST, BETTINGHUD_SMTP_USER, …)."
        )

    rec = _find_user_by_email(addr)
    if not rec:
        return True, (
            "Si un compte est associé à cette adresse, un e-mail de réinitialisation "
            "a été envoyé (vérifie aussi les spams)."
        )

    token = create_reset_token(str(rec.get("username") or ""))
    reset_path = reset_path if reset_path is not None else os.getenv("BETTINGHUD_WEB_RESET_PATH", "/")
    if not str(reset_path).startswith("/"):
        reset_path = f"/{reset_path}"
    base = web_base_url().rstrip("/")
    if reset_path == "/":
        reset_url = f"{base}/?reset_token={token}"
    else:
        reset_url = f"{base}{reset_path}?reset_token={token}"
    try:
        send_password_reset_email(
            to_email=addr,
            reset_url=reset_url,
            display_name=str(rec.get("display_name") or rec.get("username") or ""),
        )
    except Exception as exc:
        return False, f"Impossible d'envoyer l'e-mail : {exc}"

    return True, (
        "Si un compte est associé à cette adresse, un e-mail de réinitialisation "
        "a été envoyé (vérifie aussi les spams)."
    )


def complete_password_reset(token: str, new_password: str) -> tuple[bool, str]:
    from scripts.web_password_reset import consume_reset_token

    if len(new_password or "") < 4:
        return False, "Le mot de passe doit contenir au moins 4 caractères."
    uname = consume_reset_token(token)
    if not uname:
        return False, "Lien invalide ou expiré. Redemande une réinitialisation."
    if not set_user_password(uname, new_password):
        return False, "Compte introuvable."
    return True, "Mot de passe mis à jour. Tu peux te connecter."


def _reset_token_from_query() -> str:
    try:
        qp = st.query_params
        raw = qp.get("reset_token")
        if isinstance(raw, list):
            return str(raw[0] if raw else "").strip()
        return str(raw or "").strip()
    except Exception:
        return ""


def get_session_user() -> dict[str, Any] | None:
    user = st.session_state.get("web_auth_user")
    return dict(user) if isinstance(user, dict) and user.get("username") else None


def logout() -> None:
    st.session_state.pop("web_auth_user", None)
    st.session_state.pop("web_auth_mode", None)


def _render_reset_password_page(token: str) -> None:
    from scripts.web_password_reset import validate_reset_token

    st.markdown("## 🎾 BettingHUD — nouveau mot de passe")
    if not validate_reset_token(token):
        st.error("Ce lien de réinitialisation est invalide ou a expiré.")
        if st.button("Retour à la connexion", key="reset_invalid_back"):
            try:
                del st.query_params["reset_token"]
            except Exception:
                pass
            st.session_state.pop("web_auth_mode", None)
            st.rerun()
        return

    with st.form("web_reset_password_form"):
        pwd1 = st.text_input("Nouveau mot de passe", type="password", autocomplete="new-password")
        pwd2 = st.text_input("Confirmer", type="password", autocomplete="new-password")
        submitted = st.form_submit_button("Enregistrer", type="primary", width="stretch")

    if submitted:
        if pwd1 != pwd2:
            st.error("Les mots de passe ne correspondent pas.")
            return
        ok, msg = complete_password_reset(token, pwd1)
        if ok:
            try:
                del st.query_params["reset_token"]
            except Exception:
                pass
            st.success(msg)
            st.session_state.pop("web_auth_mode", None)
            if st.button("Se connecter", key="reset_ok_login", type="primary"):
                st.rerun()
        else:
            st.error(msg)


def _render_forgot_password_page() -> None:
    st.markdown("## 🎾 BettingHUD")
    st.subheader("Mot de passe oublié")
    st.caption("Saisis l'e-mail enregistré sur ton compte.")

    with st.form("web_forgot_form"):
        email = st.text_input("E-mail", autocomplete="email")
        submitted = st.form_submit_button("Envoyer le lien", type="primary", width="stretch")

    if submitted:
        _, msg = request_password_reset(email)
        st.info(msg)

    if st.button("← Retour à la connexion", key="forgot_back_login"):
        st.session_state["web_auth_mode"] = "login"
        st.rerun()


def _render_login_page() -> None:
    st.markdown("## 🎾 BettingHUD")
    st.caption("Connexion requise pour accéder au dashboard.")

    if not list_web_users():
        st.warning(
            "Aucun compte configuré. Définis `BETTINGHUD_WEB_PASSWORD_MIOUPPY` dans `.env` "
            "puis redémarre l'app, ou exécute :\n\n"
            "`py -3 scripts/init_web_user.py --username miouppy --password \"…\" --email \"…\"`"
        )

    with st.form("web_login_form", clear_on_submit=False):
        username = st.text_input(
            "Utilisateur",
            value="miouppy",
            autocomplete="username",
            help="Identifiant en minuscules : miouppy",
        )
        password = st.text_input(
            "Mot de passe",
            type="password",
            autocomplete="current-password",
            help="Sensible à la casse (majuscules / minuscules).",
        )
        submitted = st.form_submit_button("Se connecter", type="primary", width="stretch")

    if submitted:
        auth = authenticate(username, password)
        if auth:
            st.session_state["web_auth_user"] = auth
            st.rerun()
        else:
            st.error(
                "Identifiants incorrects. "
                "Utilisateur : **miouppy** (minuscules). "
                "Le mot de passe distingue les majuscules."
            )

    if st.button("Mot de passe oublié ?", key="login_forgot_link"):
        st.session_state["web_auth_mode"] = "forgot"
        st.rerun()


def require_web_login() -> dict[str, Any] | None:
    """Affiche le formulaire de connexion ou retourne l'utilisateur session."""
    sync_users_from_env()

    reset_tok = _reset_token_from_query()
    if reset_tok:
        _render_reset_password_page(reset_tok)
        return None

    user = get_session_user()
    if user:
        return user

    mode = st.session_state.get("web_auth_mode", "login")
    if mode == "forgot":
        _render_forgot_password_page()
        return None

    _render_login_page()
    return None


def render_account_banner() -> None:
    user = get_session_user()
    if not user:
        return
    c1, c2 = st.columns([4, 1])
    with c1:
        role = str(user.get("role") or "user")
        admin_tag = " · **admin**" if is_admin(user) else ""
        st.caption(
            f"👤 **{user.get('display_name') or user.get('username')}** "
            f"(`{user.get('username')}` · `{role}`{admin_tag})"
        )
    with c2:
        if st.button("Déconnexion", key="web_auth_logout_top", width="stretch"):
            logout()
            st.rerun()


def render_account_settings() -> None:
    """Bloc compte dans l'onglet Paramètres."""
    user = get_session_user()
    if not user:
        return
    st.subheader("🔐 Compte")
    st.write(
        f"Connecté en tant que **{user.get('display_name')}** "
        f"(`{user.get('username')}` · rôle `{user.get('role')}`)."
    )
    if user.get("email"):
        st.caption(f"E-mail : `{user['email']}`")
    if user.get("telegram_user_id"):
        st.caption(f"Telegram lié : `{user['telegram_user_id']}`")

    with st.expander("Changer le mot de passe"):
        with st.form("web_change_password_form"):
            current = st.text_input("Mot de passe actuel", type="password")
            new1 = st.text_input("Nouveau mot de passe", type="password")
            new2 = st.text_input("Confirmer", type="password")
            if st.form_submit_button("Mettre à jour", type="primary"):
                rec = _find_user_record(str(user.get("username") or ""))
                if not rec or not verify_password(current, str(rec.get("password_hash") or "")):
                    st.error("Mot de passe actuel incorrect.")
                elif new1 != new2:
                    st.error("Les nouveaux mots de passe ne correspondent pas.")
                elif len(new1 or "") < 4:
                    st.error("Au moins 4 caractères.")
                elif set_user_password(str(user.get("username") or ""), new1):
                    st.success("Mot de passe mis à jour.")
                else:
                    st.error("Échec de la mise à jour.")

    with st.expander("Réinitialisation par e-mail"):
        st.caption("Envoie un lien sur l'e-mail du compte (valable 1 h).")
        email_val = str(user.get("email") or "").strip()
        if not email_val:
            st.warning("Aucun e-mail sur ce compte. Contacte l'admin ou utilise init_web_user --email.")
        else:
            if st.button("M'envoyer un lien de réinitialisation", key="settings_send_reset"):
                _, msg = request_password_reset(email_val)
                st.info(msg)

    if st.button("Se déconnecter", key="web_auth_logout_settings", type="primary"):
        logout()
        st.rerun()
    st.divider()
