"""UI language selection and translation setup (Flask-Babel / gettext).

Source strings are English. A request's language is, in order: the language the
user picked (``sv_lang`` cookie), the best match for the browser's
``Accept-Language`` header, then English.
"""

from __future__ import annotations

import os

from flask import request
from flask_babel import Babel, get_locale, gettext

LANG_COOKIE = "sv_lang"
DEFAULT_LOCALE = "en"
TRANSLATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")

# Babel locale id -> (BCP 47 tag for <html lang>, native name for the switcher)
LOCALES = {
    "en": ("en", "English"),
    "de": ("de", "Deutsch"),
    "fr": ("fr", "Français"),
    "es": ("es", "Español"),
    "it": ("it", "Italiano"),
    "pt_BR": ("pt-BR", "Português (Brasil)"),
    "nl": ("nl", "Nederlands"),
    "pl": ("pl", "Polski"),
    "tr": ("tr", "Türkçe"),
    "ja": ("ja", "日本語"),
    "zh_Hans": ("zh-Hans", "简体中文"),
    "ko": ("ko", "한국어"),
}

# Accept-Language tags (lower-case) -> Babel locale id. Plain "pt" and "zh"
# fall back to the only variant we ship.
_TAG_TO_LOCALE = {
    "en": "en", "de": "de", "fr": "fr", "es": "es", "it": "it",
    "pt-br": "pt_BR", "pt": "pt_BR", "nl": "nl", "pl": "pl", "tr": "tr",
    "ja": "ja", "ko": "ko",
    "zh-hans": "zh_Hans", "zh-cn": "zh_Hans", "zh-sg": "zh_Hans", "zh": "zh_Hans",
}


def normalize_locale(value):
    """Map a cookie value or language tag to a supported Babel locale id, else None."""
    raw = str(value or "").strip()
    if raw in LOCALES:
        return raw
    return _TAG_TO_LOCALE.get(raw.replace("_", "-").lower())


def locale_from_accept_language(accept_languages):
    """Best supported locale for a werkzeug ``LanguageAccept`` value, else None.

    Tags are tried in the client's quality order; a region the app does not ship
    (e.g. ``de-AT``) falls back to its base language.
    """
    for tag, _quality in accept_languages:
        locale = normalize_locale(tag)
        if locale is None and "-" in tag:
            locale = normalize_locale(tag.split("-", 1)[0])
        if locale:
            return locale
    return None


def select_locale():
    chosen = normalize_locale(request.cookies.get(LANG_COOKIE))
    if chosen:
        return chosen
    return locale_from_accept_language(request.accept_languages) or DEFAULT_LOCALE


def current_locale_id():
    locale = get_locale()
    if locale is None:
        return DEFAULT_LOCALE
    ident = str(locale)
    if ident.startswith("zh"):
        return "zh_Hans"
    return ident if ident in LOCALES else DEFAULT_LOCALE


def N_(text):
    """Mark a string for extraction only (pybabel -k N_); translated at runtime."""
    return text


# Strings used by static JavaScript. JS looks them up with svT("English text"),
# which returns the translation from window.SV_I18N or the English text itself.
# Add every string a static .js file shows to the user here.
JS_MESSAGES = (
    N_("Loading…"),
)


def js_catalog():
    """English source -> translation for the current request's language."""
    return {text: gettext(text) for text in JS_MESSAGES}


def template_context():
    locale_id = current_locale_id()
    return {
        "current_locale": locale_id,
        "html_lang": LOCALES[locale_id][0],
        "available_locales": [(key, name) for key, (_tag, name) in LOCALES.items()],
        "js_i18n": js_catalog,
    }


def init_app(app):
    app.config.setdefault("BABEL_DEFAULT_LOCALE", DEFAULT_LOCALE)
    app.config.setdefault("BABEL_TRANSLATION_DIRECTORIES", TRANSLATIONS_DIR)
    Babel(app, locale_selector=select_locale)
    app.context_processor(template_context)
