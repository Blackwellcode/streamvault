#!/usr/bin/env python3
"""Validate the gettext catalogs in WEBUI/translations.

Checks every translated message for:
- the same %(name)s placeholders as the English source (a missing or renamed
  placeholder raises KeyError at render time),
- stray "%" characters: Flask-Babel formats every message with %, so a literal
  percent sign must be written as "%%".

Exit code 1 if any catalog has a problem. Run from the repository root:
    python scripts/check_translations.py
"""

from __future__ import annotations

import pathlib
import re
import sys

from babel.messages.pofile import read_po

ROOT = pathlib.Path(__file__).resolve().parents[1] / "WEBUI" / "translations"
PLACEHOLDER = re.compile(r"%\((\w+)\)[sd]")
VALID_PERCENT = re.compile(r"%\(\w+\)[sd]|%%")


def has_stray_percent(text: str) -> bool:
    return "%" in VALID_PERCENT.sub("", text)


def _problems(source: str, translated: str) -> list[str]:
    found = []
    if sorted(PLACEHOLDER.findall(source)) != sorted(PLACEHOLDER.findall(translated)):
        found.append("placeholders differ")
    if has_stray_percent(translated):
        found.append("stray % (use %%)")
    return found


def check_catalog(path: pathlib.Path) -> tuple[int, int, list[str]]:
    with path.open("rb") as fh:
        catalog = read_po(fh)
    total = translated = 0
    errors = []
    for message in catalog:
        if not message.id:
            continue
        total += 1
        ids = [message.id] if isinstance(message.id, str) else list(message.id)
        strings = [message.string] if isinstance(message.string, str) else list(message.string)
        if not any(strings):
            continue
        translated += 1
        for index, text in enumerate(strings):
            if not text:
                continue
            source = ids[min(index, len(ids) - 1)]
            # Plural forms may drop the count placeholder in the singular (e.g. "one file").
            for problem in _problems(source, text):
                if problem == "placeholders differ" and len(ids) > 1:
                    if set(PLACEHOLDER.findall(text)) <= set(PLACEHOLDER.findall(ids[-1])):
                        continue
                errors.append(f"{problem}: {source!r} -> {text!r}")
    return total, translated, errors


def main() -> int:
    failed = False
    for po in sorted(ROOT.glob("*/LC_MESSAGES/messages.po")):
        total, translated, errors = check_catalog(po)
        locale = po.parts[-3]
        print(f"{locale:8} {translated:5}/{total} translated, {len(errors)} problem(s)")
        for error in errors[:20]:
            print(f"    {error}")
        failed = failed or bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
