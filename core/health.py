"""Dependency-free helpers for the WebUI health endpoint."""

from __future__ import annotations


def parse_supervisor_status(stdout, expected):
    """Parse ``supervisorctl status`` output for the programs we supervise.

    Returns ``(services, missing, ok)``. Programs that are not configured in
    supervisor are reported as missing only when they are in ``expected``;
    an optional ``livebot`` program is included when present.
    """
    expected_names = {str(name) for name in expected if name}
    services = {name: {"state": "unknown"} for name in sorted(expected_names)}
    seen = set()
    for line in str(stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        program = parts[0].split(":", 1)[-1]
        if program not in expected_names and program != "livebot":
            continue
        state = parts[1]
        services[program] = {"state": state, "ok": state == "RUNNING"}
        seen.add(program)
    missing = sorted(expected_names - seen)
    ok = not missing and all(
        item.get("ok") is not False for item in services.values()
    )
    return services, missing, ok


SYSTEMD_UNIT_PREFIX = "streamvault-"
SYSTEMD_SHOW_PROPERTIES = "Id,LoadState,ActiveState,SubState"


def systemd_unit_name(program):
    return f"{SYSTEMD_UNIT_PREFIX}{program}.service"


def systemd_state_ok(active_state, sub_state):
    """Map a unit's ActiveState/SubState to the health ``ok`` flag.

    Returns True (healthy), False (degraded) or None (transitional, do not
    fail the health check on its own).
    """
    if active_state == "active":
        return sub_state in ("running", "")
    if active_state == "activating":
        # Restart=always crash loops sit in activating/auto-restart.
        return False if sub_state == "auto-restart" else None
    if active_state in ("reloading", "deactivating"):
        # deactivating covers the recorder's FFmpeg finalize window.
        return None
    return False


def parse_systemctl_show(stdout, expected):
    """Parse ``systemctl show -p Id,LoadState,ActiveState,SubState <units>``.

    Units are matched by ``Id=`` so the block order does not matter. Returns
    ``(services, missing, ok)`` like :func:`parse_supervisor_status`; a unit
    with ``LoadState=not-found`` (or no block at all) counts as missing.
    """
    expected_names = {str(name) for name in expected if name}
    services = {name: {"state": "unknown"} for name in sorted(expected_names)}
    seen = set()
    for block in str(stdout or "").replace("\r\n", "\n").split("\n\n"):
        props = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props[key.strip()] = value.strip()
        unit = props.get("Id", "")
        if not unit.startswith(SYSTEMD_UNIT_PREFIX) or not unit.endswith(".service"):
            continue
        program = unit[len(SYSTEMD_UNIT_PREFIX):-len(".service")]
        if program not in expected_names or props.get("LoadState") != "loaded":
            continue
        active = props.get("ActiveState", "unknown")
        sub = props.get("SubState", "")
        services[program] = {
            "state": f"{active}/{sub}" if sub else active,
            "ok": systemd_state_ok(active, sub),
        }
        seen.add(program)
    missing = sorted(expected_names - seen)
    ok = not missing and all(
        item.get("ok") is not False for item in services.values()
    )
    return services, missing, ok
