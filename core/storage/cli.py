"""Console interface for external storage.

Usage:
    python -m core.storage.cli list
    python -m core.storage.cli add --type google_drive --name "Mein Drive"
    python -m core.storage.cli test 1
    python -m core.storage.cli sync
    python -m core.storage.cli upload /pfad/zum/video.mp4
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

from core import database

from .base import StorageConfigError, StorageError, mask_error
from . import service
from .crypto import encryption_ready
from .registry import get_provider_class, provider_catalog


def _print(message=""):
    print(message)


def _parse_pairs(pairs):
    values = {}
    for pair in pairs or []:
        if "=" not in str(pair):
            raise StorageConfigError(f"Erwartet key=value, erhalten: {pair}")
        key, value = str(pair).split("=", 1)
        key = key.strip()
        if not key:
            raise StorageConfigError("Leerer Konfigurationsschlüssel.")
        values[key] = value
    return values


def _prompt_config(type_id: str, provided: dict) -> dict:
    from .registry import get_provider_class

    cls = get_provider_class(type_id)
    values = dict(provided)
    interactive = sys.stdin.isatty()
    for field in cls.config_fields:
        if field.key in values:
            continue
        if not interactive:
            if field.required and not field.secret:
                raise StorageConfigError(f"{field.label} fehlt (nutze --set {field.key}=...).")
            continue
        suffix = f" [{field.default}]" if field.default else ""
        if field.required:
            suffix += " *"
        prompt = f"{field.label}{suffix}: "
        if field.secret:
            value = getpass.getpass(prompt)
        else:
            value = input(prompt).strip()
        if not value and field.default:
            value = field.default
        if value:
            values[field.key] = value
    return values


def _format_timestamp(value):
    if not value:
        return "-"
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return "-"


def _cmd_list(args):
    rows = service.list_providers()
    if args.json:
        import json

        _print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        _print("Keine Cloud-Speicher konfiguriert.")
        _print("Anlegen: python -m core.storage.cli add --type google_drive --name 'Mein Drive'")
        return 0
    settings = service.get_settings()
    default_id = settings.get("default_provider_id")
    _print(f"{'ID':<4} {'Name':<26} {'Typ':<14} {'Aktiv':<6} {'Standard':<9} {'Letzter Test':<17} Status")
    for row in rows:
        status = "OK" if row.get("last_check_ok") else ("Fehler" if row.get("last_check_ok") is False else "-")
        _print(
            f"{row['id']:<4} {str(row.get('name') or '')[:26]:<26} "
            f"{str(row.get('type_label') or row.get('provider_type'))[:14]:<14} "
            f"{'ja' if row.get('enabled') else 'nein':<6} "
            f"{'ja' if row['id'] == default_id else '-':<9} "
            f"{_format_timestamp(row.get('last_check_at')):<17} {status}"
        )
    _print("")
    _print(f"Verschlüsselung: {'bereit' if encryption_ready() else 'NICHT BEREIT (VOD_SECRET_KEY setzen)'}")
    return 0


def _cmd_types(args):
    for entry in provider_catalog():
        _print(f"{entry['type_id']:<14} {entry['label']}")
        _print(f"  {entry['description']}")
        if entry["docs_url"]:
            _print(f"  Doku: {entry['docs_url']}")
    return 0


def _cmd_add(args):
    values = _parse_pairs(args.set)
    values = _prompt_config(args.type, values)
    provider_id = service.create_provider(
        args.name, args.type, values, enabled=not args.disabled
    )
    _print(f"[OK] Anbieter #{provider_id} '{args.name}' angelegt.")
    if args.no_test:
        return 0
    row = service.get_provider_record(provider_id) or {}
    cls = get_provider_class(row.get("provider_type") or args.type)
    if cls.oauth_supported and not cls.oauth_linked(row.get("config") or {}):
        _print(
            "[INFO] OAuth-Konto ist noch nicht verbunden. Der automatische "
            "Verbindungstest wurde übersprungen; nutze zuerst 'oauth-url' / "
            "'oauth-complete' (oder die WebUI)."
        )
        return 0
    result = service.test_provider(provider_id)
    _print(f"[{'OK' if result.get('ok') else 'FEHLER'}] {result.get('message')}")
    return 0 if result.get("ok") else 2


def _cmd_update(args):
    values = _parse_pairs(args.set)
    enabled = None
    if args.enable:
        enabled = True
    if args.disable:
        enabled = False
    service.update_provider(
        args.provider_id,
        name=args.name,
        provider_type=args.type,
        values=values,
        enabled=enabled,
    )
    _print(f"[OK] Anbieter #{args.provider_id} aktualisiert.")
    return 0


def _cmd_remove(args):
    row = service.get_provider_record(args.provider_id)
    if not row:
        raise StorageConfigError("Anbieter nicht gefunden.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise StorageConfigError("Löschen ohne --yes nicht möglich.")
        answer = input(
            f"Anbieter '{row.get('name')}' wirklich aus der Datenbank entfernen? "
            "Die Dateien im Cloud-Speicher bleiben erhalten. [j/N]: "
        ).strip().lower()
        if answer not in ("j", "ja", "y", "yes"):
            _print("Abgebrochen.")
            return 1
    service.delete_provider(args.provider_id)
    _print(f"[OK] Anbieter #{args.provider_id} entfernt. Cloud-Dateien wurden nicht gelöscht.")
    return 0


def _cmd_test(args):
    result = service.test_provider(args.provider_id)
    _print(f"[{'OK' if result.get('ok') else 'FEHLER'}] {result.get('message')}")
    quota = result.get("quota") or {}
    if quota:
        _print(
            f"Speicher: {service.format_bytes(quota.get('usage'))} von "
            f"{service.format_bytes(quota.get('limit'))} belegt."
        )
    return 0 if result.get("ok") else 2


def _cmd_settings(args):
    if args.set:
        values = _parse_pairs(args.set)
        settings = service.save_settings(values)
        _print("[OK] Einstellungen gespeichert.")
    else:
        settings = service.get_settings()
    for key in sorted(settings):
        _print(f"{key:<32} {settings[key]}")
    return 0


def _cmd_sync(args):
    task_id = service.enqueue_sync(args.provider, args.streamer)
    if task_id:
        _print(f"[OK] Sync eingeordnet (Task #{task_id}). Der Recorder-Worker führt ihn aus.")
    else:
        _print("[INFO] Es läuft bereits ein Sync-Task.")
    return 0


def _cmd_upload(args):
    path = str(args.path or "")
    if path and not os.path.isabs(path):
        # A path the user can see from the current shell takes precedence over
        # archive-relative notation. If it does not exist here, keep the
        # archive-relative interpretation used by `storage upload`.
        candidate = os.path.abspath(path)
        if os.path.isfile(candidate):
            path = candidate
    obj = service.enqueue_upload(path, args.provider, force=args.force)
    if obj.get("task_id"):
        _print(
            f"[OK] Upload eingeordnet (Task #{obj['task_id']}, Objekt #{obj['id']}): "
            f"{obj.get('local_path')} -> {obj.get('remote_path')}"
        )
    else:
        _print(f"[INFO] Kein neuer Upload nötig (Objekt #{obj['id']}, Status {obj.get('status')}).")
    return 0


def _cmd_objects(args):
    rows = database.list_storage_objects(
        provider_id=args.provider,
        status=args.status,
        limit=args.limit,
    )
    if not rows:
        _print("Keine Archiv-Objekte gefunden.")
        return 0
    for row in rows:
        local_state = "nur Cloud" if row.get("local_deleted_at") else "lokal"
        _print(
            f"#{row['id']:<5} {str(row.get('status') or ''):<14} "
            f"{service.format_bytes(row.get('remote_size') or row.get('local_size')):<10} "
            f"{local_state:<9} {row.get('local_path')}"
        )
        _print(f"       remote: {row.get('remote_path')}")
        if row.get("last_error"):
            _print(f"       Fehler: {row['last_error']}")
    return 0


def _cmd_restore(args):
    task_id = service.enqueue_restore(args.object_id)
    if task_id:
        _print(f"[OK] Wiederherstellung eingeordnet (Task #{task_id}).")
    else:
        _print("[INFO] Es läuft bereits eine Wiederherstellung für diese Datei.")
    return 0


def _cmd_delete_remote(args):
    obj = database.get_storage_object(args.object_id)
    if not obj:
        raise StorageConfigError("Archiv-Objekt nicht gefunden.")
    if obj.get("remote_deleted_at"):
        _print("[INFO] Die Cloud-Kopie wurde bereits gelöscht.")
        return 0
    reason = service.remote_delete_block_reason(obj)
    if reason:
        raise StorageConfigError(reason)
    if not args.yes:
        if not sys.stdin.isatty():
            raise StorageConfigError("Löschen ohne --yes nicht möglich.")
        answer = input(
            f"Cloud-Datei '{obj.get('remote_path')}' wirklich löschen? "
            "Eine lokale Kopie bleibt erhalten. [j/N]: "
        ).strip().lower()
        if answer not in ("j", "ja", "y", "yes"):
            _print("Abgebrochen.")
            return 1
    task_id = service.enqueue_delete(args.object_id)
    if task_id:
        _print(f"[OK] Löschen in der Cloud eingeordnet (Task #{task_id}).")
    else:
        _print("[INFO] Es läuft bereits ein Lösch-Task für diese Datei.")
    return 0


def _cmd_oauth_url(args):
    url = service.oauth_authorize_url(args.provider_id, args.redirect_uri)
    _print(url)
    return 0


def _cmd_oauth_complete(args):
    service.oauth_complete(args.provider_id, args.code, args.redirect_uri)
    _print(f"[OK] OAuth-Konto für Anbieter #{args.provider_id} verbunden.")
    result = service.test_provider(args.provider_id)
    _print(f"[{'OK' if result.get('ok') else 'FEHLER'}] {result.get('message')}")
    return 0 if result.get("ok") else 2


def build_parser():
    parser = argparse.ArgumentParser(
        prog="streamvault storage",
        description="Cloud-Speicher für StreamVault verwalten.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="Konfigurierte Anbieter anzeigen")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("types", help="Unterstützte Anbieter anzeigen")
    p.set_defaults(func=_cmd_types)

    p = sub.add_parser("add", help="Anbieter anlegen")
    p.add_argument("--type", required=True, choices=[entry["type_id"] for entry in provider_catalog()])
    p.add_argument("--name", required=True)
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--disabled", action="store_true")
    p.add_argument("--no-test", action="store_true")
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("update", help="Anbieter bearbeiten")
    p.add_argument("provider_id", type=int)
    p.add_argument("--name")
    p.add_argument("--type", choices=[entry["type_id"] for entry in provider_catalog()])
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--enable", action="store_true")
    p.add_argument("--disable", action="store_true")
    p.set_defaults(func=_cmd_update)

    p = sub.add_parser("remove", help="Anbieter aus der Datenbank entfernen")
    p.add_argument("provider_id", type=int)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_remove)

    p = sub.add_parser("test", help="Verbindung testen")
    p.add_argument("provider_id", type=int)
    p.set_defaults(func=_cmd_test)

    p = sub.add_parser("settings", help="Einstellungen anzeigen/ändern")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.set_defaults(func=_cmd_settings)

    p = sub.add_parser("sync", help="Archiv mit Cloud abgleichen")
    p.add_argument("--provider", type=int)
    p.add_argument("--streamer")
    p.set_defaults(func=_cmd_sync)

    p = sub.add_parser("upload", help="Einzelne Datei hochladen")
    p.add_argument("path")
    p.add_argument("--provider", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_upload)

    p = sub.add_parser("objects", help="Archiv-Objekte anzeigen")
    p.add_argument("--provider", type=int)
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_cmd_objects)

    p = sub.add_parser("restore", help="Datei aus der Cloud wiederherstellen")
    p.add_argument("object_id", type=int)
    p.set_defaults(func=_cmd_restore)

    p = sub.add_parser("delete-remote", help="Cloud-Datei löschen (lokal behalten)")
    p.add_argument("object_id", type=int)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_delete_remote)

    p = sub.add_parser("oauth-url", help="OAuth-Autorisierungs-URL ausgeben")
    p.add_argument("provider_id", type=int)
    p.add_argument("--redirect-uri", required=True)
    p.set_defaults(func=_cmd_oauth_url)

    p = sub.add_parser("oauth-complete", help="OAuth-Code einlösen")
    p.add_argument("provider_id", type=int)
    p.add_argument("--code", required=True)
    p.add_argument("--redirect-uri", required=True)
    p.set_defaults(func=_cmd_oauth_complete)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (StorageConfigError, StorageError) as exc:
        _print(f"[FEHLER] {mask_error(exc)}")
        return 2
    except KeyboardInterrupt:
        _print("\nAbgebrochen.")
        return 130
    except Exception as exc:
        _print(f"[FEHLER] {mask_error(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
