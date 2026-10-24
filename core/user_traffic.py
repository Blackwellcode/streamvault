"""Per-account usage counters for the admin traffic overview."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from core import database

_LOG = logging.getLogger("user_traffic")
_GUARD = threading.Lock()
_LAST = {}

PAGE_COOLDOWN_SEC = 45
MEDIA_COOLDOWN_SEC = 15 * 60
CHART_DAYS_DEFAULT = 30
INACTIVE_AFTER_SEC = 30 * 24 * 3600

_SKIP_PREFIXES = (
    "/static/",
    "/healthz",
    "/metrics",
    "/media/",
    "/clip/media/",
    "/_protected",
    "/chat/tube/",
    "/api/",
    "/tts/",
    "/alerts/",
    "/favicon",
    "/app-icon",
    "/sw.",
    "/manifest",
    "/admin/traffic",
    "/live/hls",
    "/hls/",
)


def _tz():
    name = str(__import__("os").environ.get("TZ") or "Europe/Berlin")
    try:
        return ZoneInfo(name)
    except Exception:
        try:
            return ZoneInfo("Europe/Berlin")
        except Exception:
            return timezone.utc


def day_key(ts=None):
    stamp = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(stamp, _tz()).strftime("%Y-%m-%d")


def _throttled(key, cooldown):
    now = time.time()
    with _GUARD:
        last = _LAST.get(key) or 0
        if now - last < cooldown:
            return False
        _LAST[key] = now
        if len(_LAST) > 4000:
            cutoff = now - max(MEDIA_COOLDOWN_SEC, PAGE_COOLDOWN_SEC)
            stale = [item for item, seen in _LAST.items() if seen < cutoff]
            for item in stale[:2000]:
                _LAST.pop(item, None)
        return True


def record(username, kind, *, key=None, ts=None):
    username = str(username or "").strip()
    kind = str(kind or "").strip().lower()
    if not username or username.lower() == "unknown" or kind not in {"login", "page", "media", "chat"}:
        return False
    if kind == "page" and not _throttled(f"page:{username.lower()}", PAGE_COOLDOWN_SEC):
        return False
    if kind == "media":
        media_key = str(key or "").strip() or "_"
        if not _throttled(f"media:{username.lower()}:{media_key}", MEDIA_COOLDOWN_SEC):
            return False
    try:
        database.increment_user_traffic(username, kind, ts=ts)
    except Exception:
        _LOG.debug("traffic record failed (%s/%s)", username, kind, exc_info=True)
        return False
    return True


def path_is_page_view(path, method="GET", *, wants_json=False):
    if str(method or "GET").upper() != "GET":
        return False
    if wants_json:
        return False
    path = str(path or "")
    lowered = path.lower()
    if any(lowered.endswith(ext) for ext in (".m3u8", ".ts", ".m4s", ".jpg", ".png", ".webp", ".js", ".css", ".map", ".woff2", ".ico")):
        return False
    return not any(path == prefix or path.startswith(prefix) for prefix in _SKIP_PREFIXES)


def maybe_record_page(username, path, method="GET", *, wants_json=False):
    if not path_is_page_view(path, method, wants_json=wants_json):
        return False
    return record(username, "page")


def format_when(ts, *, now=None):
    try:
        stamp = float(ts or 0)
    except (TypeError, ValueError):
        stamp = 0
    if stamp <= 0:
        return "nie"
    current = time.time() if now is None else float(now)
    delta = max(0, int(current - stamp))
    if delta < 120:
        return "gerade eben"
    if delta < 3600:
        return f"vor {delta // 60} Min."
    local = datetime.fromtimestamp(stamp, _tz())
    today = datetime.fromtimestamp(current, _tz()).date()
    if local.date() == today:
        return f"heute {local.strftime('%H:%M')}"
    if local.date() == today - timedelta(days=1):
        return "gestern"
    if delta < 14 * 86400:
        return f"vor {delta // 86400} Tagen"
    return local.strftime("%d.%m.%Y")


def _day_range(days):
    days = max(7, min(90, int(days or CHART_DAYS_DEFAULT)))
    today = datetime.now(_tz()).date()
    return [(today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days - 1, -1, -1)]


def _day_label(day):
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.")
    except ValueError:
        return day[5:] if len(day) >= 10 else day


def _heat_level(total, peak):
    if total <= 0 or peak <= 0:
        return 0
    ratio = total / peak
    if ratio >= 0.75:
        return 4
    if ratio >= 0.45:
        return 3
    if ratio >= 0.2:
        return 2
    return 1


def _share_pct(part, total):
    if total <= 0:
        return 0
    return int(round(100.0 * float(part) / float(total)))


def _letter_key(username):
    text = str(username or "").strip()
    if not text:
        return "#"
    first = text[0].upper()
    return first if first.isalpha() else "#"


def _chart_bars(series, plot_height=84):
    peak = max((item["total"] for item in series), default=0)
    day_count = len(series)
    slot = 16 if day_count <= 35 else 9
    bar_w = max(5, slot - 3)
    label_every = 7 if day_count <= 35 else 14
    axis_y = plot_height + 4
    chart_height = plot_height + 22
    chart_width = max(day_count * slot, 120)
    bars = []
    heat = []
    axis_labels = []
    peak_day = ""
    peak_day_total = 0
    active_days = 0

    for index, item in enumerate(series):
        x = index * slot
        day_label = _day_label(item["day"])
        total = int(item["total"] or 0)
        if total > 0:
            active_days += 1
        if total > peak_day_total:
            peak_day_total = total
            peak_day = item["day"]

        heat.append(
            {
                "day": item["day"],
                "label": day_label,
                "level": _heat_level(total, peak),
                "total": total,
                "title": (
                    f"{day_label}: keine Nutzung"
                    if total <= 0
                    else (
                        f"{day_label}: {total} "
                        f"(Login {item['logins']}, Seiten {item['pages']}, "
                        f"Video {item['media']}, Chat {item['chats']})"
                    )
                ),
            }
        )

        if index == 0 or index == day_count - 1 or index % label_every == 0:
            axis_labels.append({"x": x + bar_w / 2, "y": axis_y + 12, "text": day_label})

        if peak <= 0 or total <= 0:
            bars.append(
                {
                    "day": item["day"],
                    "x": x,
                    "w": bar_w,
                    "label": day_label,
                    "title": f"{day_label}: keine Nutzung",
                    "stacks": [],
                    "total": 0,
                }
            )
            continue

        scale = plot_height / peak
        cursor = plot_height
        stacks = []
        for kind, css in (
            ("chats", "chats"),
            ("media", "media"),
            ("pages", "pages"),
            ("logins", "logins"),
        ):
            value = int(item.get(kind) or 0)
            px = max(2, round(value * scale)) if value else 0
            if px <= 0:
                continue
            cursor -= px
            stacks.append({"kind": css, "y": max(0, cursor), "h": px, "value": value})
        bars.append(
            {
                "day": item["day"],
                "x": x,
                "w": bar_w,
                "label": day_label,
                "title": (
                    f"{day_label}: {total} "
                    f"(Login {item['logins']}, Seiten {item['pages']}, "
                    f"Video {item['media']}, Chat {item['chats']})"
                ),
                "stacks": stacks,
                "total": total,
            }
        )

    avg_active = round(sum(item["total"] for item in series) / active_days, 1) if active_days else 0.0
    return {
        "bars": bars,
        "heat": heat,
        "axis_labels": axis_labels,
        "peak": peak,
        "plot_height": plot_height,
        "chart_height": chart_height,
        "chart_width": chart_width,
        "slot": slot,
        "active_days": active_days,
        "quiet_days": max(0, day_count - active_days),
        "avg_active": avg_active,
        "peak_day": peak_day,
        "peak_day_label": _day_label(peak_day) if peak_day else "—",
        "peak_day_total": peak_day_total,
    }


def build_overview(users, *, days=CHART_DAYS_DEFAULT, now=None, inactive_after=INACTIVE_AFTER_SEC):
    day_list = _day_range(days)
    since = day_list[0]
    by_user = {}
    for row in database.list_user_traffic_daily(since):
        key = str(row["username"] or "").lower()
        by_user.setdefault(key, {})[row["day"]] = row

    current = time.time() if now is None else float(now)
    cards = []
    for username, data in (users or {}).items():
        series = []
        totals = {"logins": 0, "pages": 0, "media": 0, "chats": 0, "total": 0}
        known = by_user.get(str(username).lower(), {})
        for day in day_list:
            row = known.get(day) or {}
            point = {
                "day": day,
                "logins": int(row.get("logins") or 0),
                "pages": int(row.get("pages") or 0),
                "media": int(row.get("media") or 0),
                "chats": int(row.get("chats") or 0),
            }
            point["total"] = point["logins"] + point["pages"] + point["media"] + point["chats"]
            for field in totals:
                if field != "total":
                    totals[field] += point[field]
            totals["total"] += point["total"]
            series.append(point)
        chart = _chart_bars(series)
        last_seen = float(data.get("last_seen_at") or 0)
        last_login = float(data.get("last_login_at") or 0)
        marker = last_seen or last_login
        inactive = marker <= 0 or (current - marker) >= float(inactive_after)
        shares = {
            "logins": _share_pct(totals["logins"], totals["total"]),
            "pages": _share_pct(totals["pages"], totals["total"]),
            "media": _share_pct(totals["media"], totals["total"]),
            "chats": _share_pct(totals["chats"], totals["total"]),
        }
        cards.append(
            {
                "username": username,
                "letter": _letter_key(username),
                "role": data.get("role") or "user",
                "active": bool(data.get("active", True)),
                "discord_username": data.get("discord_username") or "",
                "last_seen_at": last_seen,
                "last_login_at": last_login,
                "last_seen_label": format_when(last_seen, now=current),
                "last_login_label": format_when(last_login, now=current),
                "inactive": inactive,
                "totals": totals,
                "shares": shares,
                "bars": chart["bars"],
                "heat": chart["heat"],
                "axis_labels": chart["axis_labels"],
                "peak": chart["peak"],
                "plot_height": chart["plot_height"],
                "chart_height": chart["chart_height"],
                "chart_width": chart["chart_width"],
                "bar_count": len(chart["bars"]),
                "active_days": chart["active_days"],
                "quiet_days": chart["quiet_days"],
                "avg_active": chart["avg_active"],
                "peak_day_label": chart["peak_day_label"],
                "peak_day_total": chart["peak_day_total"],
            }
        )

    cards.sort(key=lambda card: (str(card["username"]).casefold(), str(card["username"])))
    letters = []
    seen_letters = set()
    for card in cards:
        letter = card["letter"]
        if letter not in seen_letters:
            seen_letters.add(letter)
            letters.append(letter)
    return {
        "days": len(day_list),
        "since": since,
        "until": day_list[-1],
        "cards": cards,
        "letters": letters,
        "inactive_count": sum(1 for card in cards if card["inactive"]),
        "active_count": sum(1 for card in cards if not card["inactive"]),
    }
