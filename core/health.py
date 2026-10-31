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
