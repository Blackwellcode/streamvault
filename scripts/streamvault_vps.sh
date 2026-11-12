#!/usr/bin/env bash
# ==============================================================================
# StreamVault - VPS Deployment & Validation (Debian 12+ / Ubuntu 22.04+, Python >= 3.10)
#
#   streamvault_vps.sh validate [--run-tests] [--no-e2e] [--fix] [--strict]
#   streamvault_vps.sh deploy   (--source DIR | --ref GITREF) [--force]
#                               [--skip-tests] [--prune]
#   streamvault_vps.sh rollback
#
# Validates the Linux-only parts that the Windows audit host cannot exercise:
#   * Python runtime, compile gate, import smoke test, unit tests on Linux
#   * Symlink handling (archive symlinks, Nginx disable_symlinks, www-data
#     traversal along the resolved path, Linux-only symlink unit test)
#   * flock semantics + filesystem type for SQLite WAL
#   * Process supervisor (supervisorctl status via core.health, or systemd)
#   * Nginx X-Accel-Redirect (internal locations, alias targets, isolated
#     end-to-end test on a throwaway Nginx instance - production config is
#     never modified)
#   * HLS RAM disk (tmpfs, ownership, permissions, reboot persistence,
#     systemd sandboxing that would hide /dev/shm from Nginx)
#
# Paths default to the layout created by install.sh and can be overridden via
# SV_INSTALL_DIR, SV_APP_DIR, SV_VENV_DIR, SV_ENV_FILE, SV_SERVICE_USER,
# SV_NGINX_USER, SV_BACKUP_DIR.
#
# Exit codes: 0 = no FAIL (and no WARN with --strict), 1 = FAIL, 2 = usage.
# ==============================================================================
set -Eeuo pipefail

INSTALL_DIR="${SV_INSTALL_DIR:-/opt/streamvault}"
APP_DIR="${SV_APP_DIR:-${INSTALL_DIR}/app}"
VENV_DIR="${SV_VENV_DIR:-${INSTALL_DIR}/venv}"
ENV_FILE="${SV_ENV_FILE:-/etc/streamvault/env}"
SERVICE_USER="${SV_SERVICE_USER:-streamvault}"
NGINX_USER="${SV_NGINX_USER:-www-data}"
BACKUP_DIR="${SV_BACKUP_DIR:-/var/backups/streamvault}"
PY="${VENV_DIR}/bin/python3"
SERVICES=(webui recorder worker)
MIN_PY_MINOR=10   # Debian 12 (3.11) / Ubuntu 22.04 (3.10) floor

RUN_TESTS=0; E2E=1; FIX=0; STRICT=0; FORCE=0; SKIP_TESTS=0; PRUNE=0
SOURCE_DIR=""; GIT_REF=""

if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m'; C_GRN=$'\033[0;32m'; C_YLW=$'\033[1;33m'
    C_BLU=$'\033[0;34m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_BLD=""; C_OFF=""
fi

N_PASS=0; N_WARN=0; N_FAIL=0
pass()    { N_PASS=$((N_PASS + 1)); printf '  %s[PASS]%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn()    { N_WARN=$((N_WARN + 1)); printf '  %s[WARN]%s %s\n' "$C_YLW" "$C_OFF" "$*"; }
fail()    { N_FAIL=$((N_FAIL + 1)); printf '  %s[FAIL]%s %s\n' "$C_RED" "$C_OFF" "$*"; }
info()    { printf '  [INFO] %s\n' "$*"; }
section() { printf '\n%s== %s ==%s\n' "$C_BLU$C_BLD" "$*" "$C_OFF"; }
die()     { printf '%s[ERROR]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

# Scratch state, cleaned on every exit path.
TMP_ROOT=""
E2E_DIR=""
PROBE=""
PROBE_DIRS=()
BG_PIDS=()
cleanup() {
    local pid dir
    for pid in "${BG_PIDS[@]:-}"; do
        [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    done
    for dir in "${PROBE_DIRS[@]:-}"; do
        [[ -n "$dir" && -d "$dir" && "$dir" == *"/.sv_validate_"* ]] && rm -rf -- "$dir"
    done
    [[ -n "$TMP_ROOT" && -d "$TMP_ROOT" ]] && rm -rf -- "$TMP_ROOT"
    [[ -n "$E2E_DIR" && -d "$E2E_DIR" ]] && rm -rf -- "$E2E_DIR"
    return 0
}
trap cleanup EXIT

as_service() { runuser -u "$SERVICE_USER" -- "$@"; }
as_nginx()   { runuser -u "$NGINX_USER" -- "$@"; }
have()       { command -v "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------------------
# Environment file: parsed (never sourced) with systemd EnvironmentFile rules.
# ------------------------------------------------------------------------------
env_get() {
    local key="$1" default="${2:-}" line value
    if [[ -r "$ENV_FILE" ]]; then
        line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" | tail -n 1 || true)"
        if [[ -n "$line" ]]; then
            value="${line#*=}"
            value="${value%$'\r'}"
            if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
                value="${BASH_REMATCH[1]}"
            fi
            printf '%s' "$value"
            return 0
        fi
    fi
    printf '%s' "$default"
}
env_flag() {
    case "$(env_get "$1" "${2:-false}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

# Run Python from the app venv as the service user with the production
# environment loaded the same way systemd/supervisor would.
PY_PRELUDE='import os, sys
path = os.environ.get("SV_ENV_FILE")
try:
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"\x27":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)
except OSError as exc:
    print(f"env file not readable as service user: {exc}", file=sys.stderr)
'
py_app() {
    local code="$1"; shift
    as_service timeout "${PY_TIMEOUT:-90}" env -C "$APP_DIR" \
        PYTHONPATH="${APP_DIR}:${APP_DIR}/WEBUI" PYTHONDONTWRITEBYTECODE=1 \
        SV_ENV_FILE="$ENV_FILE" "$PY" -c "${PY_PRELUDE}${code}" "$@"
}

load_paths() {
    BASE_DIR="$(env_get VOD_BASE_DIR "$APP_DIR")"
    FILES_DIR="$(env_get VOD_FILES_DIR "${BASE_DIR}/files")"
    HLS_ROOT="$(env_get VOD_HLS_DIR "${BASE_DIR}/.hls")"
    THUMB_ROOT="$(env_get VOD_THUMB_DIR "${BASE_DIR}/.thumbs")"
    VODCACHE_DIR="${BASE_DIR}/.vodcache"
    LIVE_HLS_DIR="$(env_get LIVE_HLS_DIR "")"
    PAUSE_PCT="$(env_get STREAM_RECORDER_STORAGE_PAUSE_PCT "$(env_get STORAGE_ALERT_THRESHOLD_PCT 90)")"
    PAUSE_FREE_GB="$(env_get STREAM_RECORDER_STORAGE_PAUSE_FREE_GB "$(env_get STORAGE_ALERT_THRESHOLD_FREE_GB 20)")"
}

# Supervisor flavour: "supervisor", "systemd" or "none".
detect_supervisor() {
    # supervisorctl exits 3 when some programs are not RUNNING - still a supervisor host.
    local rc=0
    if have supervisorctl; then
        supervisorctl status >/dev/null 2>&1 || rc=$?
        if [[ $rc -eq 0 || $rc -eq 3 ]]; then echo supervisor; return; fi
    fi
    if have systemctl && systemctl list-unit-files 'streamvault-*.service' --no-legend 2>/dev/null | grep -q streamvault; then
        echo systemd; return
    fi
    echo none
}

make_probe_dir() {
    # $1 = parent directory. Creates it as the service user (inherits its umask)
    # and returns the path in $PROBE. Must not run in a subshell: the cleanup
    # trap needs the registration in PROBE_DIRS.
    PROBE="${1%/}/.sv_validate_$$"
    as_service mkdir -p "$PROBE" || return 1
    PROBE_DIRS+=("$PROBE")
}

# ==============================================================================
# Validation checks
# ==============================================================================
check_system() {
    section "System & Python-Laufzeit"
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "${ID:-}" in
            debian|ubuntu) pass "OS: ${PRETTY_NAME:-$ID}" ;;
            *) warn "OS ${PRETTY_NAME:-unbekannt} ist kein getestetes Ziel (Debian/Ubuntu)" ;;
        esac
    fi
    local cmd
    for cmd in python3 ffmpeg ffprobe nginx flock findmnt runuser curl; do
        if have "$cmd"; then pass "Befehl vorhanden: $cmd"; else fail "Befehl fehlt: $cmd"; fi
    done
    for cmd in sqlite3 rsync namei; do
        have "$cmd" || warn "Optionaler Befehl fehlt: $cmd (Diagnose/deploy)"
    done
    have fuser && info "fuser ist installiert - der Worker nutzt es bewusst nicht mehr (kein 'fuser -k -9')."

    id "$SERVICE_USER" >/dev/null 2>&1 && pass "Service-User '$SERVICE_USER' existiert" \
        || fail "Service-User '$SERVICE_USER' fehlt"
    id "$NGINX_USER" >/dev/null 2>&1 && pass "Nginx-User '$NGINX_USER' existiert" \
        || fail "Nginx-User '$NGINX_USER' fehlt (SV_NGINX_USER setzen)"

    if [[ ! -x "$PY" ]]; then
        fail "Venv-Python fehlt: $PY"
        return
    fi
    local ver
    ver="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
    if "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, ${MIN_PY_MINOR}) else 1)"; then
        pass "Venv-Python $ver (>= 3.${MIN_PY_MINOR})"
    else
        fail "Venv-Python $ver < 3.${MIN_PY_MINOR} wird nicht unterstuetzt"
    fi

    # Compile gate without writing __pycache__ into the deployed tree.
    local out
    if out="$(as_service "$PY" - "$APP_DIR" 2>&1 <<'PYEOF'
import os, sys
root = sys.argv[1]
bad, count = [], 0
for r, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d not in (".git", ".venv", "venv", "__pycache__", "node_modules")]
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(r, f)
            count += 1
            try:
                with open(p, "rb") as fh:
                    compile(fh.read(), p, "exec")
            except SyntaxError as exc:
                bad.append(f"{os.path.relpath(p, root)}:{exc.lineno}: {exc.msg}")
print(count)
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
PYEOF
)"; then
        pass "Syntax-Gate: $(head -n1 <<<"$out") Python-Dateien kompilieren"
    else
        fail "Syntax-Gate fehlgeschlagen:"; tail -n +2 <<<"$out" | sed 's/^/         /'
    fi

    local tool
    for tool in gunicorn streamlink yt-dlp; do
        if [[ -x "${VENV_DIR}/bin/${tool}" ]]; then
            pass "Venv-CLI vorhanden: ${tool}"
        else
            fail "Venv-CLI fehlt: ${VENV_DIR}/bin/${tool} (find_ytdlp/find_streamlink suchen neben sys.executable)"
        fi
    done

    # Import smoke test: STREAMVAULT_IMPORT_ONLY must keep webui side-effect free.
    if out="$(py_app '
os.environ["STREAMVAULT_IMPORT_ONLY"] = "1"
import threading
import core.health, core.database
from recorder import worker
from WEBUI import webui
bad = [t.name for t in threading.enumerate() if t.name == "twitch-badge-sync"]
print("badge-sync-thread" if bad else "no-side-effect-threads")
' 2>&1)"; then
        if grep -q "no-side-effect-threads" <<<"$out"; then
            pass "Import-Smoke-Test (core, worker, WEBUI.webui mit STREAMVAULT_IMPORT_ONLY=1) ohne Badge-Sync-Thread"
        else
            fail "WEBUI.webui startet trotz STREAMVAULT_IMPORT_ONLY den Badge-Sync-Thread"
        fi
    else
        fail "Import-Smoke-Test fehlgeschlagen:"; tail -n 8 <<<"$out" | sed 's/^/         /'
    fi
}

check_tests() {
    section "Unit-Tests auf Linux"
    if [[ ! -d "${APP_DIR}/tests" ]]; then
        warn "Kein tests/-Verzeichnis im Deployment (${APP_DIR}/tests) - Test-Gate uebersprungen"
        return
    fi
    TMP_ROOT="${TMP_ROOT:-$(mktemp -d)}"
    chmod 755 "$TMP_ROOT"
    local log="${TMP_ROOT}/unittest.log" rc=0
    as_service env -C "$APP_DIR" HOME="$TMP_ROOT" PYTHONPATH="$APP_DIR" PYTHONDONTWRITEBYTECODE=1 \
        TWITCH_BADGE_AUTO_SYNC=0 "$PY" -m unittest discover -s tests -p 'test_*.py' -v >"$log" 2>&1 || rc=$?
    local ran skipped
    ran="$(grep -Eo '^Ran [0-9]+ tests' "$log" | grep -Eo '[0-9]+' || echo 0)"
    skipped="$(grep -Eo 'skipped=[0-9]+' "$log" | grep -Eo '[0-9]+' || echo 0)"
    if [[ $rc -eq 0 ]]; then
        pass "unittest: ${ran} Tests OK (skipped=${skipped})"
    else
        fail "unittest fehlgeschlagen (rc=$rc, ${ran} Tests):"
        { grep -E '^(ERROR|FAIL):' "$log" || tail -n 15 "$log"; } | head -n 15 | sed 's/^/         /'
    fi
    if grep -E 'test_collect_media_files_skips_symlinks_and_vanishing_files .* \.\.\. ok' "$log" >/dev/null; then
        pass "Linux-Symlink-Test (auf Windows uebersprungen) laeuft und besteht"
    elif grep -E 'test_collect_media_files_skips_symlinks_and_vanishing_files .* skipped' "$log" >/dev/null; then
        fail "Symlink-Test wurde auch auf Linux uebersprungen"
    else
        warn "Symlink-Test nicht im Testlauf gefunden"
    fi
}

check_filesystem() {
    section "Dateisystem, Symlinks & Locking"
    if [[ ! -d "$BASE_DIR" ]]; then
        fail "VOD_BASE_DIR existiert nicht: $BASE_DIR"
        return
    fi
    local real fstype
    real="$(readlink -f "$BASE_DIR")"
    if [[ -L "$BASE_DIR" ]]; then
        info "VOD_BASE_DIR ist ein Symlink: $BASE_DIR -> $real (realpath-Pruefungen nutzen das Ziel)"
    fi
    fstype="$(findmnt -n -o FSTYPE -T "$real" 2>/dev/null | tail -n 1)"; fstype="${fstype:-unknown}"
    case "$fstype" in
        nfs*|cifs|smb*|fuse.sshfs|fuse.rclone|9p)
            fail "VOD_BASE_DIR liegt auf '$fstype': SQLite-WAL und flock sind dort nicht zuverlaessig" ;;
        unknown) warn "Dateisystemtyp von $real nicht ermittelbar" ;;
        *) pass "VOD_BASE_DIR auf lokalem Dateisystem ($fstype): $real" ;;
    esac

    # flock: exclusive lock must block a second non-blocking locker.
    local probe lockf holder
    if make_probe_dir "$BASE_DIR"; then
        probe="$PROBE"; lockf="${probe}/channel.lock"
        as_service touch "$lockf"
        as_service flock -x "$lockf" sleep 4 &
        holder=$!; BG_PIDS+=("$holder")
        sleep 1
        if as_service flock -n -x "$lockf" true; then
            fail "flock ist auf $fstype wirkungslos - doppelte Recorder pro Kanal moeglich"
        else
            pass "flock-Semantik funktioniert (zweiter Locker wird blockiert)"
        fi
        wait "$holder" 2>/dev/null || true
    else
        fail "Service-User kann in $BASE_DIR nicht schreiben"
    fi

    # Archive-level symlinks: worker cleanup is fail-closed for them.
    local link target n_links=0
    while IFS= read -r -d '' link; do
        n_links=$((n_links + 1))
        target="$(readlink -f "$link" || true)"
        if [[ -z "$target" || ! -e "$target" ]]; then
            fail "Verwaister Archiv-Symlink: $link -> $(readlink "$link")"
            continue
        fi
        warn "Archiv-Symlink $link -> $target: delete_folder/empty_folder werden verweigert (_is_direct_child via realpath, fail-closed)"
        if as_nginx test -r "$target" -a -x "$target"; then
            pass "Nginx-User kann Symlink-Ziel lesen: $target"
        else
            fail "Nginx-User kann Symlink-Ziel nicht lesen: $target"; namei -l "$target" 2>/dev/null | sed 's/^/         /' || true
        fi
    done < <(find "$BASE_DIR" -mindepth 1 -maxdepth 1 -type l -print0 2>/dev/null)
    [[ $n_links -eq 0 ]] && pass "Keine Archiv-Symlinks direkt unter VOD_BASE_DIR"
    local file_links
    file_links="$(find "$real" -mindepth 2 -maxdepth 2 -type l 2>/dev/null | wc -l)"
    [[ "$file_links" -gt 0 ]] && info "${file_links} Datei-Symlinks in Archiven (Cloud-Upload ueberspringt Symlinks)"
    SYMLINKS_PRESENT=$(( n_links + file_links ))

    # Nginx must traverse every directory on the resolved path.
    local d
    for d in "$real" "$HLS_ROOT" "$THUMB_ROOT"; do
        [[ -d "$d" ]] || { info "Verzeichnis existiert (noch) nicht: $d"; continue; }
        if as_nginx test -x "$d" -a -r "$d"; then
            pass "Nginx-User kann traversieren/lesen: $d"
        else
            fail "Nginx-User kann $d nicht traversieren:"; namei -l "$(readlink -f "$d")" 2>/dev/null | sed 's/^/         /' || true
        fi
    done

    # Files created by the services must be readable for X-Accel.
    local umask_val unit
    if [[ "$(detect_supervisor)" == systemd ]]; then
        for unit in recorder worker webui; do
            umask_val="$(systemctl show -p UMask --value "streamvault-${unit}.service" 2>/dev/null || true)"
            case "$umask_val" in
                0022|0002|"") pass "streamvault-${unit}: UMask=${umask_val:-default}" ;;
                *) fail "streamvault-${unit}: UMask=${umask_val} - neue Aufnahmen sind fuer ${NGINX_USER} unlesbar" ;;
            esac
        done
    fi
    local sample
    sample="$(find "$real" -mindepth 2 -maxdepth 2 -type f \( -name '*.mp4' -o -name '*.mkv' -o -name '*.ts' \) -print -quit 2>/dev/null || true)"
    if [[ -n "$sample" ]]; then
        if as_nginx head -c 1 "$sample" >/dev/null 2>&1; then
            pass "Bestehende Aufnahme fuer ${NGINX_USER} lesbar: ${sample#"$real"/}"
        else
            fail "Bestehende Aufnahme fuer ${NGINX_USER} NICHT lesbar: $sample"
        fi
    fi

    # Disk thresholds used by disk_blocks_recording().
    local used_pct free_gb
    used_pct="$(df -P "$real" | awk 'NR==2 {gsub("%","",$5); print $5}')"
    free_gb="$(df -P -k "$real" | awk 'NR==2 {printf "%.1f", $4/1048576}')"
    if awk -v u="$used_pct" -v f="$free_gb" -v p="$PAUSE_PCT" -v g="$PAUSE_FREE_GB" 'BEGIN {exit !(u >= p || f <= g)}'; then
        fail "Speicher: ${used_pct}% belegt, ${free_gb} GB frei - Recorder pausiert neue Aufnahmen (Schwellen ${PAUSE_PCT}% / ${PAUSE_FREE_GB} GB)"
    else
        pass "Speicher: ${used_pct}% belegt, ${free_gb} GB frei (Pause ab ${PAUSE_PCT}% oder <= ${PAUSE_FREE_GB} GB)"
    fi
}

check_supervisor() {
    section "Prozess-Supervisor"
    local flavour; flavour="$(detect_supervisor)"
    case "$flavour" in
    supervisor)
        local out rc=0 parsed
        out="$(supervisorctl status 2>&1)" || rc=$?
        printf '%s\n' "$out" | sed 's/^/         /'
        parsed="$(py_app '
import json
from core.health import parse_supervisor_status
services, missing, ok = parse_supervisor_status(sys.stdin.read(), {"webui", "recorder", "worker"})
print(json.dumps({"services": services, "missing": missing, "ok": ok}))
' <<<"$out" 2>/dev/null || true)"
        if [[ -z "$parsed" ]]; then
            fail "core.health.parse_supervisor_status nicht ausfuehrbar"
        else
            local name state
            for name in "${SERVICES[@]}"; do
                state="$("$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); print(d["services"].get(sys.argv[2],{}).get("state","unknown"))' "$parsed" "$name")"
                if [[ "$state" == RUNNING ]]; then pass "supervisor: $name RUNNING"; else fail "supervisor: $name $state"; fi
            done
        fi
        # /healthz calls supervisorctl as the WebUI process user.
        if as_service supervisorctl status >/dev/null 2>&1 || [[ $? -eq 3 ]]; then
            pass "supervisorctl als '$SERVICE_USER' nutzbar (/healthz sieht Dienststatus)"
        else
            warn "supervisorctl als '$SERVICE_USER' nicht nutzbar (Socket-Rechte) - /healthz meldet services.ok=null, falls die WebUI nicht als root laeuft"
        fi
        # Supervisor default stopwaitsecs=10 kills the recorder before the 30 s FFmpeg finalize.
        local conf sw
        conf="$(grep -lE '^\[program:recorder\]' /etc/supervisor/conf.d/*.conf /etc/supervisor/supervisord.conf /etc/supervisord.conf 2>/dev/null | head -n1 || true)"
        if [[ -n "$conf" ]]; then
            sw="$(awk '/^\[program:recorder\]/{f=1;next} /^\[/{f=0} f && /^stopwaitsecs/{gsub(/[^0-9]/,"",$0); print}' "$conf")"
            if [[ -z "$sw" || "$sw" -lt 45 ]]; then
                warn "[program:recorder] stopwaitsecs=${sw:-10 (Default)} < 45: SIGKILL vor FFMPEG_FINALIZE_TIMEOUT (30 s) -> abgeschnittene MP4s ($conf)"
            else
                pass "[program:recorder] stopwaitsecs=$sw"
            fi
        fi
        ;;
    systemd)
        local unit state enabled restarts tstop priv
        for unit in "${SERVICES[@]}"; do
            state="$(systemctl is-active "streamvault-${unit}.service" 2>/dev/null || true)"
            enabled="$(systemctl is-enabled "streamvault-${unit}.service" 2>/dev/null || true)"
            restarts="$(systemctl show -p NRestarts --value "streamvault-${unit}.service" 2>/dev/null || echo 0)"
            if [[ "$state" == active ]]; then pass "systemd: streamvault-${unit} active ($enabled)"; else fail "systemd: streamvault-${unit} $state"; fi
            [[ "${restarts:-0}" -gt 5 ]] && warn "streamvault-${unit}: ${restarts} Neustarts - moegliche Crash-Schleife (journalctl -u streamvault-${unit})"
            priv="$(systemctl show -p PrivateDevices --value "streamvault-${unit}.service" 2>/dev/null || true)"
            [[ "$priv" == yes ]] && fail "streamvault-${unit}: PrivateDevices=yes isoliert /dev/shm - Nginx sieht keine Live-HLS-Segmente"
        done
        tstop="$(systemctl show -p TimeoutStopUSec --value streamvault-recorder.service 2>/dev/null || true)"
        info "streamvault-recorder TimeoutStopSec=${tstop:-?} (muss > FFMPEG_FINALIZE_TIMEOUT=30 s sein)"
        ;;
    *)
        fail "Weder supervisorctl noch streamvault-*.service gefunden"
        ;;
    esac
}

# Extract "internal alias" pairs for a location from `nginx -T`.
nginx_location() {
    # $1 = dump file, $2 = location prefix -> prints "<internal:0|1> <alias>"
    awk -v loc="$2" '
        $1 == "location" && ($2 == loc || ($2 == "^~" && $3 == loc)) { inblk = 1; depth = 0; internal = 0; alias = ""; found = 1 }
        inblk {
            n = gsub(/\{/, "{"); m = gsub(/\}/, "}"); depth += n - m
            if ($1 == "internal;") internal = 1
            if ($1 == "alias") { alias = $2; sub(/;$/, "", alias) }
            if (depth <= 0 && (n > 0 || m > 0)) { print internal, alias; inblk = 0; exit }
        }
        END { if (!found) print "missing" }
    ' "$1"
}

check_nginx() {
    section "Nginx & X-Accel-Redirect"
    if ! have nginx; then fail "nginx nicht installiert"; return; fi
    if nginx -t >/dev/null 2>&1; then pass "nginx -t"; else fail "nginx -t:"; { nginx -t 2>&1 || true; } | sed 's/^/         /'; return; fi
    TMP_ROOT="${TMP_ROOT:-$(mktemp -d)}"
    local dump="${TMP_ROOT}/nginx.dump"
    nginx -T >"$dump" 2>/dev/null

    if env_flag VOD_USE_X_ACCEL true; then pass "VOD_USE_X_ACCEL=true"; else warn "VOD_USE_X_ACCEL ist aus - Flask streamt Medien selbst"; fi

    local -a prefixes=(
        "$(env_get VOD_X_ACCEL_PREFIX /_protected)|$BASE_DIR"
        "$(env_get VOD_FILES_ACCEL_PREFIX /_protected_files)|$FILES_DIR"
        "$(env_get VOD_CACHE_ACCEL_PREFIX /_protected_vodcache)|$VODCACHE_DIR"
        "$(env_get VOD_THUMB_ACCEL_PREFIX /_protected_thumbs)|$THUMB_ROOT"
        "$(env_get VOD_HLS_ACCEL_PREFIX /_protected_hls)|$HLS_ROOT"
        "$(env_get LIVE_HLS_ACCEL_PREFIX /_protected_hls_live)|${LIVE_HLS_DIR:-/dev/shm/streamvault_hls}"
    )
    local entry prefix expected res internal alias
    for entry in "${prefixes[@]}"; do
        prefix="${entry%%|*}/"; prefix="${prefix//\/\//\/}"; expected="${entry#*|}"
        res="$(nginx_location "$dump" "$prefix")"
        if [[ "$res" == missing ]]; then fail "location $prefix fehlt"; continue; fi
        internal="${res%% *}"; alias="${res#* }"
        [[ "$internal" == 1 ]] && pass "location $prefix ist internal" \
            || fail "location $prefix ist NICHT internal - Archiv waere ohne Login abrufbar"
        if [[ -z "$alias" ]]; then fail "location $prefix ohne alias"; continue; fi
        [[ "$alias" == */ ]] || fail "alias '$alias' in $prefix ohne abschliessenden '/' (Pfad-Verkettung bricht)"
        if [[ "$(readlink -m "${alias%/}")" == "$(readlink -m "${expected%/}")" ]]; then
            pass "alias $prefix -> $alias"
        else
            fail "alias $prefix -> $alias, erwartet ${expected%/}/ (aus $ENV_FILE)"
        fi
    done

    local ds
    ds="$({ grep -E '^[[:space:]]*disable_symlinks[[:space:]]' "$dump" || true; } | awk '{print $2}' | tr -d ';' | sort -u | tr '\n' ' ')"
    if [[ -n "$ds" && "$ds" != "off " ]]; then
        if [[ "${SYMLINKS_PRESENT:-0}" -gt 0 ]]; then
            fail "disable_symlinks ${ds}aktiv, aber Archive enthalten Symlinks -> X-Accel liefert 403/404"
        else
            warn "disable_symlinks ${ds}aktiv - spaetere Archiv-Symlinks (z. B. Block-Storage) wuerden blockiert"
        fi
    else
        pass "disable_symlinks off (Nginx folgt Archiv-Symlinks)"
    fi

    local has_tls=0
    grep -Eq '^[[:space:]]*listen[[:space:]].*(443|ssl)' "$dump" && has_tls=1
    if env_flag VOD_SECURE_COOKIES false; then
        [[ $has_tls -eq 1 ]] && pass "VOD_SECURE_COOKIES=true mit TLS-Listener" \
            || fail "VOD_SECURE_COOKIES=true ohne TLS-Listener - Login-Redirect-Schleife auf Port 80"
    else
        [[ $has_tls -eq 1 ]] && warn "TLS aktiv, aber VOD_SECURE_COOKIES=false - nach HTTPS-Umstellung auf true setzen" \
            || pass "VOD_SECURE_COOKIES=false passend zu HTTP-only"
    fi

    # Live checks against the production vhost (read-only requests).
    local probe code
    if make_probe_dir "$BASE_DIR"; then
        probe="$PROBE"
        as_service bash -c "printf 'svprobe' > '${probe}/probe.mp4'"
        code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: localhost' "http://127.0.0.1/_protected/$(basename "$probe")/probe.mp4" || true)"; code="${code:-000}"
        case "$code" in
            404) pass "Direkter Zugriff auf /_protected/... -> 404 (internal greift)" ;;
            200) fail "Direkter Zugriff auf /_protected/... -> 200: Archiv oeffentlich!" ;;
            000) warn "Nginx auf 127.0.0.1:80 nicht erreichbar" ;;
            *)   warn "Direkter Zugriff auf /_protected/... -> HTTP $code (erwartet 404)" ;;
        esac
        if as_nginx head -c 1 "${probe}/probe.mp4" >/dev/null 2>&1; then
            pass "Neu angelegte Datei des Service-Users ist fuer ${NGINX_USER} lesbar"
        else
            fail "Neu angelegte Datei des Service-Users ist fuer ${NGINX_USER} NICHT lesbar (umask/ACL)"
        fi
    fi
    code="$(curl -s -o "${TMP_ROOT}/healthz.json" -w '%{http_code}' -H 'Host: localhost' http://127.0.0.1/healthz || true)"; code="${code:-000}"
    case "$code" in
        200) pass "GET /healthz ueber Nginx -> 200 $(head -c 160 "${TMP_ROOT}/healthz.json")" ;;
        503) fail "GET /healthz -> 503 degraded $(head -c 160 "${TMP_ROOT}/healthz.json")" ;;
        *)   fail "GET /healthz ueber Nginx -> HTTP $code (WebUI/Proxy pruefen)" ;;
    esac

    if [[ $E2E -eq 1 ]]; then xaccel_e2e; fi
    return 0
}

# Isolated X-Accel round trip: throwaway Nginx (same worker user, same alias
# targets, same disable_symlinks) in front of a stub backend that only emits
# X-Accel-Redirect. Production config and services are not touched.
xaccel_e2e() {
    local probe live_probe port_ng port_be dir conf ds
    make_probe_dir "$BASE_DIR" || { warn "E2E: kein Probe-Verzeichnis"; return 0; }
    probe="$PROBE"
    as_service bash -c "head -c 65536 /dev/urandom > '${probe}/e2e.mp4'"
    if [[ -n "$LIVE_HLS_DIR" && -d "$LIVE_HLS_DIR" ]]; then
        live_probe=""
        make_probe_dir "$LIVE_HLS_DIR" && live_probe="$PROBE"
        [[ -n "$live_probe" ]] && as_service bash -c "printf '#EXTM3U\n' > '${live_probe}/index.m3u8'"
    fi
    dir="$(mktemp -d)"; chmod 755 "$dir"; E2E_DIR="$dir"
    read -r port_ng port_be < <("$PY" -c 'import socket
s=[socket.socket() for _ in range(2)]
[x.bind(("127.0.0.1",0)) for x in s]
print(*[x.getsockname()[1] for x in s])')
    ds="$(grep -hE '^[[:space:]]*disable_symlinks[[:space:]]' "${TMP_ROOT}/nginx.dump" | head -n1 || true)"
    local name live_name="" live_loc=""
    name="$(basename "$probe")"
    if [[ -n "${live_probe:-}" ]]; then
        live_name="$(basename "$live_probe")"
        live_loc="location /_protected_hls_live/ {
            internal; alias ${LIVE_HLS_DIR%/}/;
            types { application/vnd.apple.mpegurl m3u8; video/mp2t ts; }
            add_header Cache-Control \"no-cache, no-store, must-revalidate\" always;
        }"
    fi
    conf="${dir}/nginx.conf"
    cat >"$conf" <<NGX
user ${NGINX_USER};
worker_processes 1;
daemon off;
pid ${dir}/nginx.pid;
error_log ${dir}/error.log warn;
events { worker_connections 64; }
http {
    access_log off;
    client_body_temp_path ${dir}/body; proxy_temp_path ${dir}/proxy;
    fastcgi_temp_path ${dir}/fcgi; uwsgi_temp_path ${dir}/uwsgi; scgi_temp_path ${dir}/scgi;
    ${ds}
    server {
        listen 127.0.0.1:${port_ng};
        location / { proxy_pass http://127.0.0.1:${port_be}; }
        location /_protected/ { internal; alias ${BASE_DIR%/}/; }
        ${live_loc}
    }
}
NGX
    # Stub backend only redirects to the probe files (no open file proxy).
    "$PY" -c '
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
ALLOWED = set(sys.argv[2:])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        target = self.path.replace("/accel", "", 1)
        if target not in ALLOWED:
            self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers()
            return
        self.send_response(200)
        self.send_header("X-Accel-Redirect", target)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
' "$port_be" "/_protected/${name}/e2e.mp4" ${live_name:+"/_protected_hls_live/${live_name}/index.m3u8"} >"${dir}/backend.log" 2>&1 &
    BG_PIDS+=("$!")
    nginx -c "$conf" -p "$dir" &
    BG_PIDS+=("$!")
    sleep 1.5

    local code size headers
    code="$(curl -s -o "${dir}/out.bin" -w '%{http_code} %{size_download}' "http://127.0.0.1:${port_ng}/accel/_protected/${name}/e2e.mp4" || true)"; code="${code:-000 0}"
    size="${code#* }"; code="${code%% *}"
    if [[ "$code" == 200 && "$size" == 65536 ]] && cmp -s "${dir}/out.bin" "${probe}/e2e.mp4"; then
        pass "X-Accel E2E: /_protected/ liefert Datei als ${NGINX_USER} bytegenau aus (alias ${BASE_DIR%/}/)"
    else
        fail "X-Accel E2E: /_protected/ -> HTTP ${code}, ${size} Bytes"; tail -n 5 "${dir}/error.log" 2>/dev/null | sed 's/^/         /' || true
    fi
    code="$(curl -s -o /dev/null -w '%{http_code}' -r 100-199 "http://127.0.0.1:${port_ng}/accel/_protected/${name}/e2e.mp4" || true)"; code="${code:-000}"
    [[ "$code" == 206 ]] && pass "X-Accel E2E: Range-Request -> 206 (Seeking im Player)" \
        || fail "X-Accel E2E: Range-Request -> HTTP $code (erwartet 206)"
    if [[ -n "${live_probe:-}" ]]; then
        headers="$(curl -s -D - -o /dev/null "http://127.0.0.1:${port_ng}/accel/_protected_hls_live/${live_name}/index.m3u8" || true)"
        if grep -q '^HTTP/1.1 200' <<<"$headers" && grep -qi 'content-type: application/vnd.apple.mpegurl' <<<"$headers"; then
            pass "X-Accel E2E: Live-HLS aus RAM-Disk (m3u8, korrekter MIME-Typ)"
        else
            fail "X-Accel E2E: Live-HLS aus ${LIVE_HLS_DIR} nicht auslieferbar:"; head -n 3 <<<"$headers" | sed 's/^/         /'
        fi
    fi
    rm -rf -- "$dir"
}

check_hls_ramdisk() {
    section "HLS RAM-Disk"
    if [[ -z "$LIVE_HLS_DIR" ]]; then
        warn "LIVE_HLS_DIR fehlt in $ENV_FILE - WebUI und Recorder nutzen den Default /dev/shm/streamvault_hls; Nginx-Alias muss dazu passen"
        LIVE_HLS_DIR="/dev/shm/streamvault_hls"
    fi
    pass "LIVE_HLS_DIR=$LIVE_HLS_DIR"
    if [[ ! -d "$LIVE_HLS_DIR" ]]; then
        if [[ $FIX -eq 1 ]]; then
            install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$LIVE_HLS_DIR" && pass "LIVE_HLS_DIR angelegt (--fix)"
        else
            fail "LIVE_HLS_DIR existiert nicht (nach Reboot geloescht?) - mit --fix anlegen"
            return
        fi
    fi
    local fstype owner avail_mb
    fstype="$(findmnt -n -o FSTYPE -T "$LIVE_HLS_DIR" 2>/dev/null | tail -n 1)"; fstype="${fstype:-unknown}"
    [[ "$fstype" == tmpfs ]] && pass "LIVE_HLS_DIR liegt auf tmpfs" \
        || warn "LIVE_HLS_DIR liegt auf '$fstype' statt tmpfs - Segmente erzeugen Disk-I/O"
    owner="$(stat -c %U "$LIVE_HLS_DIR")"
    [[ "$owner" == "$SERVICE_USER" ]] && pass "Owner: $owner" || warn "Owner von LIVE_HLS_DIR ist $owner (erwartet $SERVICE_USER)"
    local probe
    if make_probe_dir "$LIVE_HLS_DIR"; then
        probe="$PROBE"
        as_service bash -c "printf 'x' > '${probe}/seg0.ts'" && pass "Service-User kann Segmente schreiben"
        if as_nginx head -c 1 "${probe}/seg0.ts" >/dev/null 2>&1; then
            pass "${NGINX_USER} kann Segmente lesen"
        else
            fail "${NGINX_USER} kann Segmente in $LIVE_HLS_DIR nicht lesen"; namei -l "${probe}/seg0.ts" 2>/dev/null | sed 's/^/         /' || true
        fi
    else
        fail "Service-User kann nicht in $LIVE_HLS_DIR schreiben"
    fi
    avail_mb="$(df -P -k "$LIVE_HLS_DIR" | awk 'NR==2 {printf "%d", $4/1024}')"
    if [[ "$avail_mb" -lt 256 ]]; then fail "RAM-Disk: nur ${avail_mb} MB frei"
    elif [[ "$avail_mb" -lt 1024 ]]; then warn "RAM-Disk: ${avail_mb} MB frei (< 1 GB, mehrere Live-Kanaele knapp)"
    else pass "RAM-Disk: ${avail_mb} MB frei"; fi

    if grep -rqsF "$LIVE_HLS_DIR" /etc/tmpfiles.d /usr/lib/tmpfiles.d 2>/dev/null; then
        pass "tmpfiles.d-Eintrag vorhanden (Verzeichnis ueberlebt Reboot)"
    elif [[ $FIX -eq 1 ]]; then
        printf 'd %s 0755 %s %s -\n' "$LIVE_HLS_DIR" "$SERVICE_USER" "$SERVICE_USER" > /etc/tmpfiles.d/streamvault.conf
        systemd-tmpfiles --create /etc/tmpfiles.d/streamvault.conf && pass "tmpfiles.d-Eintrag angelegt (--fix)"
    else
        warn "Kein tmpfiles.d-Eintrag: /dev/shm wird beim Reboot geleert (mit --fix anlegen)"
    fi
    local stale
    stale="$(find "$LIVE_HLS_DIR" -mindepth 2 -maxdepth 2 -name '*.m3u8' -mmin +10 2>/dev/null | wc -l)"
    [[ "$stale" -gt 0 ]] && info "${stale} Playlists aelter als 10 min in der RAM-Disk (cleanup_live_hls raeumt beim naechsten Lauf)"
    return 0
}

check_config() {
    section "Konfiguration & Sicherheit"
    if [[ ! -f "$ENV_FILE" ]]; then fail "$ENV_FILE fehlt"; return; fi
    local mode owner group
    mode="$(stat -c %a "$ENV_FILE")"; owner="$(stat -c %U "$ENV_FILE")"; group="$(stat -c %G "$ENV_FILE")"
    if [[ "${mode: -1}" != 0 ]]; then fail "$ENV_FILE ist world-readable ($mode)"
    elif [[ "$mode" == 640 && "$owner" == root && "$group" == "$SERVICE_USER" ]]; then pass "$ENV_FILE 640 root:$SERVICE_USER"
    else warn "$ENV_FILE: $mode $owner:$group (erwartet 640 root:$SERVICE_USER)"; fi
    [[ -n "$(env_get VOD_PASS)" ]] && pass "VOD_PASS gesetzt" || fail "VOD_PASS leer"
    local sk; sk="$(env_get VOD_SECRET_KEY)"
    [[ ${#sk} -ge 32 ]] && pass "VOD_SECRET_KEY >= 32 Zeichen" || fail "VOD_SECRET_KEY fehlt/zu kurz"
    if env_flag SENTRY_SEND_DEFAULT_PII false; then warn "SENTRY_SEND_DEFAULT_PII=true - PII geht an Sentry"; else pass "Sentry-PII deaktiviert"; fi
    return 0
}

do_validate() {
    [[ $EUID -eq 0 ]] || die "validate muss als root laufen (runuser/nginx -T)."
    load_paths
    printf '%sStreamVault VPS-Validierung%s  app=%s venv=%s env=%s\n' "$C_BLD" "$C_OFF" "$APP_DIR" "$VENV_DIR" "$ENV_FILE"
    check_system
    [[ $RUN_TESTS -eq 1 ]] && check_tests
    check_config
    check_filesystem
    check_supervisor
    check_hls_ramdisk
    check_nginx
    section "Ergebnis"
    printf '  PASS=%d  WARN=%d  FAIL=%d\n' "$N_PASS" "$N_WARN" "$N_FAIL"
    if [[ $N_FAIL -gt 0 ]]; then printf '  %sNO-GO%s\n' "$C_RED$C_BLD" "$C_OFF"; return 1; fi
    if [[ $STRICT -eq 1 && $N_WARN -gt 0 ]]; then printf '  %sNO-GO (--strict)%s\n' "$C_YLW$C_BLD" "$C_OFF"; return 1; fi
    printf '  %sGO%s\n' "$C_GRN$C_BLD" "$C_OFF"
    return 0
}

# ==============================================================================
# Deployment
# ==============================================================================
restart_services() {
    case "$(detect_supervisor)" in
        supervisor) supervisorctl restart worker webui recorder ;;
        systemd)    systemctl restart streamvault-worker.service streamvault-webui.service streamvault-recorder.service ;;
        *)          die "Kein Supervisor erkannt - Dienste nicht neu gestartet." ;;
    esac
}

services_healthy() {
    local unit
    case "$(detect_supervisor)" in
        supervisor)
            for unit in "${SERVICES[@]}"; do
                supervisorctl status "$unit" 2>/dev/null | grep -q RUNNING || return 1
            done ;;
        systemd)
            for unit in "${SERVICES[@]}"; do
                systemctl is-active --quiet "streamvault-${unit}.service" || return 1
            done ;;
        *) return 1 ;;
    esac
}

gate_release() {
    # Compile + tests with the venv interpreter as the service user.
    N_FAIL=0
    check_system
    [[ $SKIP_TESTS -eq 1 ]] || check_tests
    [[ $N_FAIL -eq 0 ]]
}

# Code copies and snapshots never carry runtime data, databases or secrets.
# One list for install.sh, deploy, snapshot and rollback.
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXCLUDES_FILE="${SCRIPTS_DIR}/deploy-excludes.txt"
CODE_EXCLUDES=(--exclude-from="$EXCLUDES_FILE")
# shellcheck source=recording-guard.sh
source "${SCRIPTS_DIR}/recording-guard.sh"

# Usage: snapshot_code SRC_DIR ARCHIVE.tgz
# Copies SRC_DIR through the shared exclude list into a staging directory and
# packs that, so the snapshot contains exactly what a deploy would copy.
snapshot_code() {
    local src="$1" archive="$2" stage rc=0
    stage="$(mktemp -d)"
    rsync -a "${CODE_EXCLUDES[@]}" "${src%/}"/ "$stage"/ || rc=$?
    if [[ $rc -eq 0 ]]; then
        tar -czf "$archive" -C "$stage" . || rc=$?
    fi
    rm -rf -- "$stage"
    return $rc
}

# Consistent online copy of the SQLite database (WAL-safe) before any deploy.
backup_database() {
    local ts="$1" db tmp out
    db="$(readlink -f "${BASE_DIR}")/streamrecorder.db"
    if [[ ! -f "$db" ]]; then
        printf 'DB-Backup: keine Datenbank unter %s\n' "$db"
        return 0
    fi
    have sqlite3 || die "sqlite3 fehlt (apt-get install sqlite3) - ohne DB-Backup kein Deploy."
    # Run as the service user so no root-owned -wal/-shm files appear next to the DB.
    tmp="$(mktemp -d)"
    chown "$SERVICE_USER" "$tmp"
    if ! as_service sqlite3 "$db" ".backup '${tmp}/db.sqlite'"; then
        rm -rf -- "$tmp"
        die "DB-Backup von $db fehlgeschlagen - Deploy abgebrochen."
    fi
    if [[ "$(sqlite3 "${tmp}/db.sqlite" 'PRAGMA integrity_check;' 2>&1)" != "ok" ]]; then
        rm -rf -- "$tmp"
        die "DB-Backup von $db besteht integrity_check nicht - Deploy abgebrochen."
    fi
    out="${BACKUP_DIR}/streamrecorder-${ts}.db"
    mv -- "${tmp}/db.sqlite" "$out"
    chmod 600 "$out"
    rm -rf -- "$tmp"
    find "$BACKUP_DIR" -maxdepth 1 -name 'streamrecorder-*.db' -printf '%T@ %p\n' | sort -rn | tail -n +6 | cut -d' ' -f2- | xargs -r rm -f --
    printf 'DB-Backup: %s\n' "$out"
}

restore_snapshot() {
    local state="${BACKUP_DIR}/last_deploy.env" mode prev archive tmp
    [[ -r "$state" ]] || die "Kein Deploy-Zustand in $state."
    mode="$(grep '^MODE=' "$state" | cut -d= -f2-)"
    prev="$(grep '^PREV_REV=' "$state" | cut -d= -f2-)"
    archive="$(grep '^SNAPSHOT=' "$state" | cut -d= -f2-)"
    if [[ "$mode" == git && -n "$prev" ]]; then
        as_service git -C "$APP_DIR" checkout --quiet --detach "$prev"
        printf 'Rollback: git checkout %s\n' "$prev"
    else
        [[ -r "$archive" ]] || die "Snapshot fehlt: $archive"
        tmp="$(mktemp -d)"
        tar -xzf "$archive" -C "$tmp"
        rsync -a --delete "${CODE_EXCLUDES[@]}" "$tmp"/ "$APP_DIR"/
        chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
        rm -rf -- "$tmp"
        printf 'Rollback: %s wiederhergestellt\n' "$archive"
    fi
}

do_deploy() {
    [[ $EUID -eq 0 ]] || die "deploy muss als root laufen."
    [[ -n "$SOURCE_DIR" || -n "$GIT_REF" ]] || die "deploy braucht --source DIR oder --ref GITREF."
    [[ -n "$SOURCE_DIR" && -n "$GIT_REF" ]] && die "--source und --ref schliessen sich aus."
    [[ -d "$APP_DIR" && -x "$PY" ]] || die "Keine Installation unter $APP_DIR / $VENV_DIR - zuerst install.sh ausfuehren."
    have rsync || die "rsync fehlt (apt-get install rsync)."
    [[ -r "$EXCLUDES_FILE" ]] || die "$EXCLUDES_FILE fehlt."
    load_paths

    # 1. Never cut running recordings without explicit consent.
    # Fail closed (scripts/recording-guard.sh): unreadable archive = maybe recording.
    sv_recording_guard "$BASE_DIR" "$FORCE" \
        || die "Recorder-Neustart abgebrochen (laufende Aufnahmen oder Archiv nicht pruefbar). Spaeter erneut versuchen oder --force."

    # 2. Snapshot current code.
    install -d -m 0750 "$BACKUP_DIR"
    local ts mode prev snapshot
    ts="$(date +%Y%m%d-%H%M%S)"
    snapshot="${BACKUP_DIR}/app-${ts}.tgz"
    snapshot_code "$APP_DIR" "$snapshot" || die "Code-Snapshot nach $snapshot fehlgeschlagen."
    if [[ -d "${APP_DIR}/.git" ]]; then
        mode=git; prev="$(as_service git -C "$APP_DIR" rev-parse HEAD)"
    else
        mode=copy; prev=""
    fi
    [[ -n "$SOURCE_DIR" ]] && mode=copy
    printf 'MODE=%s\nPREV_REV=%s\nSNAPSHOT=%s\nTS=%s\n' "$mode" "$prev" "$snapshot" "$ts" > "${BACKUP_DIR}/last_deploy.env"
    find "$BACKUP_DIR" -maxdepth 1 -name 'app-*.tgz' -printf '%T@ %p\n' | sort -rn | tail -n +6 | cut -d' ' -f2- | xargs -r rm -f --
    printf 'Snapshot: %s\n' "$snapshot"
    backup_database "$ts"

    # 3. Update code.
    if [[ -n "$GIT_REF" ]]; then
        [[ -d "${APP_DIR}/.git" ]] || die "$APP_DIR ist kein git-Checkout - --source verwenden."
        if [[ -n "$(as_service git -C "$APP_DIR" status --porcelain --untracked-files=no)" ]]; then
            die "Lokale Aenderungen in $APP_DIR - Deploy abgebrochen."
        fi
        as_service git -C "$APP_DIR" fetch --quiet --tags origin
        as_service git -C "$APP_DIR" checkout --quiet --detach "$GIT_REF"
        printf 'Code: git %s -> %s\n' "${prev:0:12}" "$(as_service git -C "$APP_DIR" rev-parse --short=12 HEAD)"
    else
        [[ -f "${SOURCE_DIR}/WEBUI/webui.py" ]] || die "$SOURCE_DIR ist kein StreamVault-Quellbaum."
        local -a rs=(-a "${CODE_EXCLUDES[@]}")
        if [[ $PRUNE -eq 1 ]]; then
            case "$(readlink -f "$BASE_DIR")/" in
                "$(readlink -f "$APP_DIR")"/*) die "--prune verweigert: VOD_BASE_DIR liegt innerhalb von APP_DIR." ;;
            esac
            rs+=(--delete)
        fi
        rsync "${rs[@]}" "${SOURCE_DIR%/}"/ "$APP_DIR"/
        chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
        printf 'Code: rsync aus %s%s\n' "$SOURCE_DIR" "$([[ $PRUNE -eq 1 ]] && echo ' (--prune)')"
    fi

    # 4. Dependencies.
    as_service "${VENV_DIR}/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

    # 5. Release gate before touching running services.
    if ! gate_release; then
        printf '%sRelease-Gate fehlgeschlagen - Rollback.%s\n' "$C_RED" "$C_OFF"
        restore_snapshot
        exit 1
    fi

    # 6. Restart and verify.
    restart_services
    sleep 10
    if ! services_healthy; then
        printf '%sDienste nach Neustart nicht gesund - Rollback.%s\n' "$C_RED" "$C_OFF"
        restore_snapshot
        restart_services
        exit 1
    fi
    N_PASS=0; N_WARN=0; N_FAIL=0
    RUN_TESTS=0
    do_validate
}

do_rollback() {
    [[ $EUID -eq 0 ]] || die "rollback muss als root laufen."
    restore_snapshot
    restart_services
    sleep 10
    services_healthy && printf 'Rollback abgeschlossen, Dienste laufen.\n' || die "Dienste nach Rollback nicht gesund."
}

usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

# Sourcing the script (tests) only defines the functions above.
[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0

CMD="${1:-}"; [[ -n "$CMD" ]] || usage; shift
while [[ $# -gt 0 ]]; do
    case "$1" in
        --run-tests)  RUN_TESTS=1 ;;
        --no-e2e)     E2E=0 ;;
        --fix)        FIX=1 ;;
        --strict)     STRICT=1 ;;
        --force)      FORCE=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        --prune)      PRUNE=1 ;;
        --source)     SOURCE_DIR="${2:?--source braucht ein Verzeichnis}"; shift ;;
        --ref)        GIT_REF="${2:?--ref braucht einen git-Ref}"; shift ;;
        -h|--help)    usage ;;
        *) printf 'Unbekannte Option: %s\n' "$1" >&2; usage ;;
    esac
    shift
done

case "$CMD" in
    validate) do_validate ;;
    deploy)   do_deploy ;;
    rollback) do_rollback ;;
    *) usage ;;
esac
