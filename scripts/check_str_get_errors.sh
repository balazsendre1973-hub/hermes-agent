#!/usr/bin/env bash
# scripts/check_str_get_errors.sh
#
# Egészség-számláló a "str → dict" tool-argumentum coercion hibajellegère
# (AttributeError: 'str' object has no attribute 'get').
#
# Számolja az agent naplóban az „object has no attribute 'get'” előfordulásainak
# számát (grep -c), és csak az UTÓLÓ futástól újonnan belépőket jelzi, így a
# 06:00/18:00-es egészség-cron napi új találatokat kap.
#
# Kimenet: „<n> új találat (összesen: <c>)”. Ha n != 0: „FIGYELMEZTETÉS” előtag
# és kilépési kód 1 (cron-értesítésre használható). Tiszta naplóra 0 új találat,
# kilépési kód 0.
#
# Környezeti változók:
#   HERMES_AGENT_LOG        napló elérési útja (alap: /home/endre/.hermes/logs/agent.log)
#   HERMES_COERCION_STATE_DIR  állapotfájl mappája (alap: /home/endre/.hermes/state)
#
# Cron beépítés (crontab -e):
#   0 6,18 * * * /path/to/repo/scripts/check_str_get_errors.sh \
#       >> /home/endre/.hermes/logs/coercion_health.log 2>&1
set -u

LOG_FILE="${HERMES_AGENT_LOG:-/home/endre/.hermes/logs/agent.log}"
STATE_DIR="${HERMES_COERCION_STATE_DIR:-/home/endre/.hermes/state}"
STATE_FILE="${STATE_DIR}/check_str_get_errors.last_count"
PATTERN="object has no attribute 'get'"

mkdir -p "$STATE_DIR" || exit 2

if [[ ! -f "$LOG_FILE" ]]; then
    echo "FIGYELMEZTETÉS: agent napló nem elérhető ($LOG_FILE), coercion-ellenőrzés nem lehetséges."
    exit 2
fi

# grep -c a megfelelő sorok számát adja vissza (0 ha nincs találat).
current="$(grep -c -- "$PATTERN" "$LOG_FILE" 2>/dev/null || true)"
current="${current//[^0-9]/}"
current="${current:-0}"

last=0
if [[ -f "$STATE_FILE" ]]; then
    last="$(cat "$STATE_FILE" 2>/dev/null || true)"
    last="${last//[^0-9]/}"
    last="${last:-0}"
fi

new=$((current - last))
if (( new < 0 )); then
    # Napló rotáció / újrakezdés: az egész aktuális tartalom számít újonnan belépőnek.
    new="$current"
fi

# Atomikus állapotfrissítés.
tmp="${STATE_FILE}.tmp.$$"
printf '%s\n' "$current" > "$tmp" && mv "$tmp" "$STATE_FILE"

if (( new > 0 )); then
    echo "FIGYELMEZTETÉS: $new új 'object has no attribute \\"get\\"' találat az agent naplójában (összesen: $current, az utolsó ellenőrzéstől új: $new)."
    exit 1
fi
echo "0 új 'object has no attribute \\"get\\"' találat (összesen: $current)."
exit 0
