# Contributing to StreamVault

Thanks for helping. This file covers how to get a working checkout, the rules
the code base relies on, and what a pull request needs.

## Development setup

StreamVault targets Linux in production (Ubuntu 22.04 / Debian 12) but the
test suite also runs on Windows and macOS.

```bash
git clone https://github.com/Blackwellcode/streamvault.git
cd streamvault
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt ruff
```

Run the app locally:

```bash
export VOD_USER=admin VOD_PASS='choose-a-password' \
       VOD_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
       VOD_BASE_DIR="$PWD/data"
python -m WEBUI.webui --host 127.0.0.1 --port 8080
```

## Checks before you open a pull request

CI runs exactly these on Python 3.10, 3.11 and 3.12:

```bash
python -m compileall -q core recorder WEBUI bin run_transcribe.py tests
ruff check .
python -m unittest discover -s tests -v
```

## Rules the code base depends on

- **Python 3.10 compatible.** No PEP 701 f-strings (no quotes, backslashes or
  comments inside `{...}`), no `tomllib`, `datetime.UTC`, `typing.Self` or
  `except*`. Python 3.12 accepts PEP 701 silently, so run the compile step on
  3.10 or rely on CI.
- **No silent failures.** No `except Exception: pass`. Catch the specific
  error class and log it; if a fallback value is returned, the log must say so.
- **Fail closed around recordings.** Anything that deletes, remuxes or moves
  media must treat "unknown" as "still recording".
- **Tests are not weakened to go green.** Fix the code instead. New behaviour
  gets a test.
- **Stop before unlock.** When stopping a channel, stop the child process
  before releasing its `flock`. Waits are bounded; never `fuser -k`.
- **Paths from clients** are validated against `config.BASE_DIR`
  (`_is_direct_child`, `safe_join`) before touching the file system.

## Translations

The UI source language is English; catalogs live in `WEBUI/translations/<locale>/LC_MESSAGES/messages.po`.

- In templates use `{{ _('Text') }}`, in Python `gettext("Text")` (or `lazy_gettext` for
  module-level constants; `_` is a throwaway variable in `webui.py`), in static JS
  `svT('Text')` and register the string in `JS_MESSAGES` in `WEBUI/i18n.py`.
- Placeholders are named: `_('%(count)s VODs', count=n)`. A literal percent sign is `%%`.
- After changing strings, refresh and compile the catalogs:

```bash
pybabel extract -F babel.cfg -k lazy_gettext -k N_ --no-location --sort-output -o WEBUI/translations/messages.pot .
pybabel update -i WEBUI/translations/messages.pot -d WEBUI/translations --no-fuzzy-matching
pybabel compile -d WEBUI/translations
python scripts/check_translations.py
```

- Improving a translation: edit the `msgstr` in the locale's `messages.po`, then compile.
  Commit both the `.po` and the `.mo` file; CI checks that they match.
- Legal texts (`content/agb.md`, `content/datenschutz.md`) are German originals with English
  translations (`*.en.md`); other languages show the English version.

## Pull requests

- One topic per pull request; small, focused commits.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`fix(worker): ...`, `feat(webui): ...`), written in English.
- Describe what changed for operators: new environment variables, migrations,
  or anything that needs a manual step goes into `CHANGELOG.md` under
  *Unreleased*.
- UI changes: include a screenshot at desktop and phone width.

## Reporting security issues

Please do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
