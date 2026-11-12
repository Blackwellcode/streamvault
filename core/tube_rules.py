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

# English translation for the web UI in every non-German language. The German
# text above stays binding; keep both lists in the same order and length.
RULES_EN = {
    "title": "StreamVault access: binding rules",
    "accept_phrase": "ACCEPT",
    "footer": "Note: these rules are house rules for this group. They do not replace third-party rights.",
    "items": [
        "**Access for approved members only.** Do not share accounts, invites or login details. Anyone who wants to bring someone asks the approver first; no self-invites.",
        "**No public traces.** No screenshots, clips, links or mentions on other Discord servers, Reddit, Twitter/X, Twitch chat, forums and so on.",
        "**Links stay internal.** StreamVault links, share tokens and invites stay on this server and in approved channels. No forwarding to non-members.",
        "**No sharing of files.** VODs, downloads, chat exports and screen recordings must not go to cloud services, other servers or third parties.",
        "**No bulk downloads, no mirrors.** No mass downloading, no second archive, no re-hosting.",
        "**No monetisation.** No donations, ads, paid access or giveaways with access as a prize.",
        "**No \"official\" framing.** Do not claim the project is authorised or tolerated by the streamer.",
        "**Do not share technical details.** No hosting, bot, admin or token details outside the admin group.",
        "**Report problems.** Suspected leak or public post: tell the admins immediately.",
        "**Violation = immediate removal.** Access revoked, possibly kick/ban.",
    ],
}


def current_version() -> str:
    return RULES_VERSION


def accept_phrase(lang: str | None = None) -> str:
    """Phrase shown to the user; German in German, English in every other language."""
    if lang and lang != "de":
        return RULES_EN["accept_phrase"]
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


def rules_web_context(*, site_url: str = "", lang: str | None = None) -> dict[str, Any]:
    site = (site_url or "").rstrip("/")
    english = bool(lang and lang != "de")

    def _md_bold_to_html(text: str) -> str:
        import html as _html
        import re

        escaped = _html.escape(text)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)

    items = RULES_EN["items"] if english else RULES_ITEMS
    return {
        "rules_title": RULES_EN["title"] if english else RULES_TITLE,
        "rules_items": [_md_bold_to_html(item) for item in items],
        "rules_footer": RULES_EN["footer"] if english else RULES_FOOTER,
        "rules_version": RULES_VERSION,
        "rules_accept_phrase": accept_phrase(lang),
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
    # Either language's phrase counts: the user may switch the UI language mid-way.
    if normalize_accept_input(raw) not in (RULES_ACCEPT_PHRASE, RULES_EN["accept_phrase"]):
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


def status_for_user_row(row: dict | None) -> dict[str, Any]:
    row = row or {}
    return {
        "rules_version": str(row.get("rules_version") or ""),
        "rules_accepted_at": float(row.get("rules_accepted_at") or 0),
        "rules_ok": user_has_accepted_current_rules(row),
        "required_version": RULES_VERSION,
    }
