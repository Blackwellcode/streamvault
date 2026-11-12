# shellcheck shell=bash
# Shared "are recordings running?" check for install.sh, bin/streamvault and
# scripts/streamvault_vps.sh. Source it; it defines functions only.
#
# A fresh .part file (written within the last 2 minutes) directly below an
# archive folder means ffmpeg is recording. The check fails closed: if the
# archive cannot be scanned completely, recordings may be running.

# Usage: sv_active_recordings BASE_DIR
# Prints the fresh .part files. Returns non-zero when find could not read the
# whole archive (its error message goes to stderr).
sv_active_recordings() {
    local base="$1"
    [[ -n "$base" && -d "$base" ]] || return 0
    find "$(readlink -f "$base")" -mindepth 2 -maxdepth 2 -name '*.part' -mmin -2
}

# Usage: sv_recording_guard BASE_DIR FORCE(0|1)
# Returns 0 when it is safe to stop the recorder (or FORCE=1),
#         1 when recordings are running,
#         2 when the archive could not be scanned.
sv_recording_guard() {
    local base="$1" force="${2:-0}" active=""
    if ! active="$(sv_active_recordings "$base")"; then
        if [[ "$force" != "1" ]]; then
            echo "[ERROR] Could not check ${base} for active recordings (see the find error above)." >&2
            echo "        Fix the permissions or force the operation." >&2
            return 2
        fi
        echo "[!] Active-recording check failed; continuing because it was forced." >&2
    fi
    if [[ -n "$active" ]]; then
        echo "[!] Active recordings (fresh .part files):" >&2
        echo "$active" | sed 's/^/    /' >&2
        if [[ "$force" != "1" ]]; then
            echo "[ERROR] Restarting the recorder now would split these recordings." >&2
            return 1
        fi
        echo "[!] Continuing because it was forced." >&2
    fi
    return 0
}
