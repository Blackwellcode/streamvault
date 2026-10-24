#!/usr/bin/env python3
"""NachtSilo Tube AGB — single source of truth.

Web (`/agb` + consent gate) and Discord (`#agb`, `/agb sync`) read the same
markdown file. Bump `termsVersion` in content/agb.md whenever the binding
text changes so users must re-accept.

Maintenance: see docs/agb.md.
"""

from __future__ import annotations

import html as html_lib
import os
import re
import time
from functools import lru_cache
from typing import Any

from core import database
from core import tube_rules
from core.config import APP_DIR

# Canonical copies live in the repo content/ directory. Do not resolve them
# next to this module: core/content only had a partial symlink after the split.
AGB_PATH = os.path.join(APP_DIR, "content", "agb.md")
PRIVACY_PATH = os.path.join(APP_DIR, "content", "datenschutz.md")
LOGIN_AGB_HINT = (os.environ.get("VOD_PUBLIC_BASE") or "http://localhost:5000").rstrip("/") + "/agb"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_FORBIDDEN_PHRASES = (
    "fair use",
    "privat = erlaubt",
    "nur freunde = legal",
    "mit erlaubnis des streamers",
    "autorisiert vom streamer",
)


def _parse_frontmatter(raw: str, *, path: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"Datei ohne Frontmatter: {path}")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, match.group(2).strip() + "\n"


def _load_document() -> dict[str, Any]:
    with open(AGB_PATH, encoding="utf-8") as fh:
        raw = fh.read()
    meta, body = _parse_frontmatter(raw, path=AGB_PATH)
    version = str(meta.get("termsVersion") or "").strip()
    if not version:
        raise ValueError("content/agb.md: termsVersion fehlt")
    lowered = raw.lower()
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in lowered:
            raise ValueError(f"Verbotene AGB-Formulierung: {phrase}")
    if "[todo:" in lowered:
        raise ValueError("content/agb.md enthält noch [TODO: …] — Lücken schließen oder Impressum-Ausnahme prüfen")
    return {
        "terms_version": version,
        "effective_date": str(meta.get("effectiveDate") or "").strip(),
        "title": str(meta.get("title") or "Allgemeine Nutzungsbedingungen – NachtSilo Tube").strip(),
        "body_md": body,
        "raw": raw,
    }


@lru_cache(maxsize=1)
def _cached_document(mtime_ns: int) -> dict[str, Any]:
    return _load_document()


def load_document(*, force: bool = False) -> dict[str, Any]:
    if force:
        _cached_document.cache_clear()
    try:
        mtime_ns = os.stat(AGB_PATH).st_mtime_ns
        return _cached_document(mtime_ns)
    except OSError:
        return {
            "terms_version": "v1.0",
            "effective_date": "2026-01-01",
            "title": "Nutzungsbedingungen – NachtSilo Hub",
            "body_md": "# Nutzungsbedingungen\n\nNutzung nur für autorisierte Benutzer.",
            "raw": "",
        }


def current_version() -> str:
    return load_document()["terms_version"]


def effective_date() -> str:
    return load_document()["effective_date"]


def terms_title() -> str:
    return load_document()["title"]


def _md_inline(text: str) -> str:
    escaped = html_lib.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def markdown_to_html(body: str) -> str:
    """Minimal markdown → HTML for AGB (headings, lists, paragraphs)."""
    lines = body.replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    list_type: str | None = None
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            parts.append("<p>" + _md_inline(" ".join(paragraph)) + "</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            parts.append(f"</{list_type}>")
            list_type = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            flush_paragraph()
            close_list()
            continue
        heading = re.match(r"^(#{2,3})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            parts.append(f'<h{level} id="{html_lib.escape(slug)}">{_md_inline(title)}</h{level}>')
            continue
        ul_item = re.match(r"^[-*]\s+(.+)$", line)
        if ul_item:
            flush_paragraph()
            if list_type != "ul":
                close_list()
                parts.append("<ul>")
                list_type = "ul"
            parts.append(f"<li>{_md_inline(ul_item.group(1))}</li>")
            continue
        close_list()
        paragraph.append(line.strip())
    flush_paragraph()
    close_list()
    return "\n".join(parts)


def terms_html() -> str:
    return markdown_to_html(load_document()["body_md"])


def terms_plain_text() -> str:
    doc = load_document()
    lines = [doc["title"], "", f"Version: {doc['terms_version']}", f"Wirksam: {doc['effective_date']}", ""]
    for raw in doc["body_md"].splitlines():
        text = raw.replace("**", "")
        lines.append(text)
    return "\n".join(lines).strip() + "\n"


def terms_discord_messages(*, site_url: str = "") -> list[str]:
    """Kurzfassung für Discord (<=1900). Volltext bleibt im Login-Bereich."""
    site = (site_url or os.environ.get("VOD_PUBLIC_BASE", "http://localhost:5000")).rstrip("/")
    doc = load_document()
    version = doc["terms_version"]
    body = (
        f"**{doc['title']}**\n"
        f"Version `{version}` · wirksam ab `{doc['effective_date']}`\n"
        "\n"
        "Kurzfassung (kein Ersatz für den Volltext):\n"
        "• Invite-only — Zugang nur nach Freigabe; Zugänge nicht weitergeben.\n"
        "• Nur persönlicher Gebrauch im freigegebenen Kreis; keine Weitergabe nach außen.\n"
        "• Kein Nutzungsrecht gegenüber Rechteinhabern oder Plattformen.\n"
        "• Verstoß kann zum sofortigen Entzug des Zugangs führen.\n"
        "\n"
        f"Volltext nur im Login-Bereich: {site}/agb\n"
        "Hausordnung (Anlage): <#CHANNEL_RULES>\n"
        f"Zustimmung: eine Checkbox/Phrase (`{tube_rules.accept_phrase()}`) = AGB **inkl.** Nutzungsregeln.\n"
        "Discord-Reaktion „gelesen“ ersetzt **keine** Account-Freigabe.\n"
        "\n"
        "_Diese AGB regeln nur das Verhältnis zwischen Betreiber und Nutzer. "
        "Sie schaffen keine Rechte an Inhalten Dritter und ersetzen keine gesetzlichen Pflichten._\n"
        "_Diese AGB ersetzen keine anwaltliche Prüfung._"
    )
    if len(body) > 1900:
        body = body[:1890].rstrip() + "…"
    return [body]


@lru_cache(maxsize=1)
def _cached_privacy(mtime_ns: int) -> dict[str, Any]:
    with open(PRIVACY_PATH, encoding="utf-8") as fh:
        raw = fh.read()
    meta, body = _parse_frontmatter(raw, path=PRIVACY_PATH)
    version = str(meta.get("privacyVersion") or "").strip()
    if not version:
        raise ValueError("content/datenschutz.md: privacyVersion fehlt")
    return {
        "privacy_version": version,
        "effective_date": str(meta.get("effectiveDate") or "").strip(),
        "title": str(meta.get("title") or "Datenschutzerklärung – NachtSilo Tube").strip(),
        "body_md": body,
        "raw": raw,
    }


def load_privacy_document(*, force: bool = False) -> dict[str, Any]:
    if force:
        _cached_privacy.cache_clear()
    try:
        mtime_ns = os.stat(PRIVACY_PATH).st_mtime_ns
    except OSError as exc:
        raise FileNotFoundError(f"Datenschutz-Datei fehlt: {PRIVACY_PATH}") from exc
    return _cached_privacy(mtime_ns)


def privacy_html() -> str:
    return markdown_to_html(load_privacy_document()["body_md"])


def privacy_web_context(*, site_url: str = "") -> dict[str, Any]:
    site = (site_url or "").rstrip("/")
    doc = load_privacy_document()
    return {
        "privacy_title": doc["title"],
        "privacy_version": doc["privacy_version"],
        "privacy_effective_date": doc["effective_date"],
        "privacy_html": privacy_html(),
        "privacy_url": f"{site}/datenschutz" if site else "/datenschutz",
    }


def terms_web_context(*, site_url: str = "") -> dict[str, Any]:
    site = (site_url or "").rstrip("/")
    doc = load_document()
    rules_ctx = tube_rules.rules_web_context(site_url=site)
    privacy = privacy_web_context(site_url=site)
    return {
        "terms_title": doc["title"],
        "terms_version": doc["terms_version"],
        "terms_effective_date": doc["effective_date"],
        "terms_html": terms_html(),
        "terms_url": f"{site}/agb" if site else "/agb",
        "terms_accept_phrase": tube_rules.accept_phrase(),
        "rules_title": rules_ctx["rules_title"],
        "rules_items": rules_ctx["rules_items"],
        "rules_footer": rules_ctx["rules_footer"],
        "rules_version": rules_ctx["rules_version"],
        "privacy_url": privacy["privacy_url"],
        "privacy_version": privacy["privacy_version"],
    }


def user_has_accepted_current_terms(row: dict | None) -> bool:
    if not row:
        return False
    version = str(row.get("terms_version") or "").strip()
    accepted_at = row.get("terms_accepted_at")
    try:
        accepted_ts = float(accepted_at or 0)
    except (TypeError, ValueError):
        accepted_ts = 0.0
    return bool(version == current_version() and accepted_ts > 0)


def user_has_accepted_current_consent(row: dict | None) -> bool:
    """AGB + eingebettete Hausordnung müssen aktuell akzeptiert sein."""
    return user_has_accepted_current_terms(row) and tube_rules.user_has_accepted_current_rules(row)


def record_terms_acceptance(
    username: str,
    *,
    version: str | None = None,
    source: str = "web",
    discord_id: str | None = None,
) -> dict | None:
    username = str(username or "").strip()
    if not username:
        return None
    ver = str(version or current_version()).strip() or current_version()
    now = time.time()
    row = database.get_user(username) or {}
    did = str(discord_id if discord_id is not None else row.get("discord_id") or "").strip()
    database.set_user_terms_acceptance(
        username,
        terms_version=ver,
        terms_accepted_at=now,
        rules_version=tube_rules.current_version(),
        rules_accepted_at=now,
    )
    database.add_terms_acceptance(
        username=username,
        discord_id=did,
        terms_version=ver,
        rules_version=tube_rules.current_version(),
        accepted_at=now,
        source=source,
    )
    return database.get_user(username)


def status_for_user_row(row: dict | None) -> dict[str, Any]:
    row = row or {}
    rules = tube_rules.status_for_user_row(row)
    return {
        "terms_version": str(row.get("terms_version") or ""),
        "terms_accepted_at": float(row.get("terms_accepted_at") or 0),
        "terms_ok": user_has_accepted_current_terms(row),
        "required_version": current_version(),
        "consent_ok": user_has_accepted_current_consent(row),
        "rules_version": rules["rules_version"],
        "rules_accepted_at": rules["rules_accepted_at"],
        "rules_ok": rules["rules_ok"],
        "rules_required_version": rules["required_version"],
    }
