#!/usr/bin/env python3
"""Envoi d'e-mails (SMTP) pour BettingHUD web."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any


def smtp_configured() -> bool:
    host = (os.getenv("BETTINGHUD_SMTP_HOST") or "").strip()
    user = (os.getenv("BETTINGHUD_SMTP_USER") or "").strip()
    return bool(host and user)


def web_base_url() -> str:
    return (os.getenv("BETTINGHUD_WEB_BASE_URL") or "http://localhost:8501").rstrip("/")


def _smtp_settings() -> dict[str, Any]:
    host = (os.getenv("BETTINGHUD_SMTP_HOST") or "").strip()
    port = int((os.getenv("BETTINGHUD_SMTP_PORT") or "587").strip() or "587")
    user = (os.getenv("BETTINGHUD_SMTP_USER") or "").strip()
    password = os.getenv("BETTINGHUD_SMTP_PASSWORD") or ""
    use_tls = (os.getenv("BETTINGHUD_SMTP_USE_TLS") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    from_addr = (os.getenv("BETTINGHUD_SMTP_FROM") or user or "").strip()
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "use_tls": use_tls,
        "from_addr": from_addr,
    }


def send_email(*, to: str, subject: str, body_text: str, body_html: str | None = None) -> None:
    cfg = _smtp_settings()
    if not cfg["host"] or not cfg["from_addr"]:
        raise RuntimeError(
            "SMTP non configuré : définis BETTINGHUD_SMTP_HOST, BETTINGHUD_SMTP_USER "
            "et BETTINGHUD_SMTP_FROM dans .env"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = to
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    if cfg["use_tls"]:
        context = ssl.create_default_context()
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as smtp:
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)


def send_password_reset_email(
    *,
    to_email: str,
    reset_url: str,
    display_name: str,
) -> None:
    name = display_name or "utilisateur"
    subject = "BettingHUD — réinitialisation du mot de passe"
    body_text = (
        f"Bonjour {name},\n\n"
        "Tu as demandé une réinitialisation de mot de passe pour BettingHUD.\n"
        f"Ouvre ce lien (valable 1 heure) :\n\n{reset_url}\n\n"
        "Si tu n'as pas fait cette demande, ignore cet e-mail.\n\n"
        "— BettingHUD"
    )
    body_html = f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;line-height:1.5">
<p>Bonjour <strong>{name}</strong>,</p>
<p>Tu as demandé une réinitialisation de mot de passe pour <strong>BettingHUD</strong>.</p>
<p><a href="{reset_url}">Réinitialiser mon mot de passe</a></p>
<p style="color:#666;font-size:0.9em">Lien valable 1 heure. Si tu n'as pas fait cette demande, ignore cet e-mail.</p>
</body></html>"""
    send_email(to=to_email, subject=subject, body_text=body_text, body_html=body_html)
