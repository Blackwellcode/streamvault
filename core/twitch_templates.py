"""Jinja templates for NachtSilo Twitch live hub and embed player."""

import os

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "WEBUI", "templates")


def _load_html_template(name, fallback=""):
    path = os.path.join(_TEMPLATES_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return fallback


def _watch_desktop_template():
    return (
        _load_html_template("twitch_watch.html")
        + _load_html_template("twitch_watch_clip_markup.html")
        + _load_html_template("twitch_watch_proxy.js.html")
        + _load_html_template("twitch_watch_chat.js.html")
        + _load_html_template("twitch_watch_clip.js.html")
        + """
</body>
</html>
"""
    )


def _watch_mobile_template():
    return (
        _load_html_template("twitch_watch_mobile.html")
        + _load_html_template("twitch_watch_clip_markup.html")
        + _load_html_template("twitch_watch_proxy.js.html")
        + _load_html_template("twitch_watch_chat.js.html")
        + _load_html_template("twitch_watch_clip.js.html")
        + """
</body>
</html>
"""
    )


def get_live_hub_template():
    return _load_html_template("twitch_live_hub.html")


def get_watch_template(*, mobile=False):
    return _watch_mobile_template() if mobile else _watch_desktop_template()


# Backwards-compatible names (fresh load each attribute access is not possible for
# module constants — prefer get_watch_template / get_live_hub_template).
TWITCH_LIVE_HUB_TEMPLATE = get_live_hub_template()
TWITCH_WATCH_TEMPLATE = _watch_desktop_template()
TWITCH_WATCH_MOBILE_TEMPLATE = _watch_mobile_template()
