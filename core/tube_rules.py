#!/usr/bin/env python3
"""StreamVault Hausordnung — single source of truth for the short rules.

Shared by webui (Anlage in /agb).
Legal frame is content/agb.md via tube_terms.py. Consent is one checkbox/phrase
for AGB inkl. Nutzungsregeln. Bump RULES_VERSION when RULES_ITEMS change;
also bump termsVersion in content/agb.md so users must re-accept.

Maintenance: edit RULES_ITEMS / RULES_VERSION here only. See docs/regeln.md
and docs/agb.md.
"""

from __future__ import annotations

import os
from typing import Any

# Bump this string whenever RULES_ITEMS change in a binding way.
RULES_VERSION = "2026-09-07-v1"
RULES_TITLE = "StreamVault / Zugang — verbindliche Regeln"
RULES_ACCEPT_PHRASE = "AKZEPTIERE"
RULES_FOOTER = (
    "Hinweis: Diese Regeln sind Hausordnung für den Kreis. "
    "Sie ersetzen keine Rechte Dritter."
)

RULES_ITEMS: list[str] = [
    "**Zugang nur für Freigeschaltete.** Accounts, Invites und Login-Daten nicht weitergeben. Wer jemanden mitbringt, fragt vorher den Approver — kein Self-Invite.",
    "**Keine öffentlichen Spuren.** Keine Screenshots, Clips, Links oder Mentions in anderen Discords, Reddit, Twitter/X, Twitch-Chat, Foren usw.",
    "**Links nur intern.** StreamVault-Links, Share-Tokens und Einladungen bleiben in diesem Server / freigegebenen Kanälen. Kein Forwarding an Nicht-Mitglieder.",
    "**Keine Weitergabe von Dateien.** VODs, Downloads, Chat-Exporte, Screen-Recordings: nicht in Clouds, andere Server oder an Dritte.",
    "**Kein Download-Bulk / kein Spiegel.** Kein Massen-Download, kein zweites Archiv, kein Re-Host.",
    "**Keine Monetisierung.** Keine Spenden, Ads, Paid Access, Giveaways mit Zugang als Preis.",
    "**Kein „offiziell“-Framing.** Nicht behaupten, das Projekt sei autorisiert oder vom Streamer geduldet.",
    "**Technik nicht teilen.** Keine Hosting-/Bot-/Admin-/Token-Details außerhalb der Admin-Runde.",
    "**Bei Problemen melden.** Leak-Verdacht oder öffentlicher Post → sofort Admins.",
    "**Verstoß = sofortiger Entzug.** ACL weg, ggf. Kick/Ban.",
]


def current_version() -> str:
    return RULES_VERSION


def accept_phrase() -> str:
    return RULES_ACCEPT_PHRASE


def rules_plain_lines() -> list[str]:
    lines = [RULES_TITLE, ""]
    for idx, item in enumerate(RULES_ITEMS, start=1):
        # Strip markdown bold for plain contexts.
        text = item.replace("**", "")
        lines.append(f"{idx}. {text}")
    lines.append("")
    lines.append(RULES_FOOTER.replace("Hinweis: ", ""))
    lines.append(f"Version: {RULES_VERSION}")
    return lines


def rules_web_context(*, site_url: str = "") -> dict[str, Any]:
    site = (site_url or "").rstrip("/")

    def _md_bold_to_html(text: str) -> str:
        import html as _html
        import re

        escaped = _html.escape(text)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)

    return {
        "rules_title": RULES_TITLE,
        "rules_items": [_md_bold_to_html(item) for item in RULES_ITEMS],
        "rules_footer": RULES_FOOTER,
        "rules_version": RULES_VERSION,
        "rules_accept_phrase": RULES_ACCEPT_PHRASE,
        "rules_url": f"{site}/agb" if site else "/agb",
    }


def rules_discord_messages(*, site_url: str = "") -> list[str]:
    """Split into Discord-safe message bodies (<=1900 chars each)."""
    site = (site_url or os.environ.get("VOD_PUBLIC_BASE", "http://localhost:5000")).rstrip("/")
    header = f"**{RULES_TITLE}**\n"
    chunks: list[str] = []
    body = header
    for idx, item in enumerate(RULES_ITEMS, start=1):
        piece = f"{idx}. {item}\n"
        if len(body) + len(piece) > 1800:
            chunks.append(body.rstrip())
            body = piece
        else:
            body += piece
    footer = (
        f"\n_{RULES_FOOTER}_\n"
        f"Version `{RULES_VERSION}` · Web-AGB (Login): {site}/agb\n"
        f"Zustimmung: AGB inkl. Nutzungsregeln — Feld mit `{RULES_ACCEPT_PHRASE}` ausfüllen."
    )
    if len(body) + len(footer) > 1900:
        chunks.append(body.rstrip())
        chunks.append(footer.strip())
    else:
        chunks.append((body + footer).rstrip())
    return chunks


def normalize_accept_input(raw: str | None) -> str:
    return " ".join(str(raw or "").strip().upper().split())


def validate_accept_input(raw: str | None) -> None:
    if normalize_accept_input(raw) != RULES_ACCEPT_PHRASE:
        raise ValueError(
            f"AGB inkl. Nutzungsregeln nicht akzeptiert. Bitte exakt `{RULES_ACCEPT_PHRASE}` eingeben "
            f"(siehe #regeln / #agb bzw. {LOGIN_RULES_HINT})."
        )


LOGIN_RULES_HINT = (os.environ.get("VOD_PUBLIC_BASE") or "http://localhost:5000").rstrip("/") + "/agb"


def user_has_accepted_current_rules(row: dict | None) -> bool:
    if not row:
        return False
    version = str(row.get("rules_version") or "").strip()
    accepted_at = row.get("rules_accepted_at")
    try:
        accepted_ts = float(accepted_at or 0)
    except (TypeError, ValueError):
        accepted_ts = 0.0
    return bool(version == RULES_VERSION and accepted_ts > 0)


def record_rules_acceptance(username: str, *, version: str | None = None) -> dict | None:
    """Record Hausordnung acceptance; AGB consent is kept in lockstep (Anlage A)."""
    from core import tube_terms

    username = str(username or "").strip()
    if not username:
        return None
    return tube_terms.record_terms_acceptance(username, source="rules")


def status_for_user_row(row: dict | None) -> dict[str, Any]:
    row = row or {}
    return {
        "rules_version": str(row.get("rules_version") or ""),
        "rules_accepted_at": float(row.get("rules_accepted_at") or 0),
        "rules_ok": user_has_accepted_current_rules(row),
        "required_version": RULES_VERSION,
    }
