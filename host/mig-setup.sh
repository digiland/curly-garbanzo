#!/usr/bin/env bash
# Recreate the MIG layout named in /etc/default/nvidia-mig-layout (default:
# 7 x 1g.18gb). Idempotent: safe to run repeatedly, and does nothing when the
# GPU already has the requested instances. MIG *mode* survives reboot; the
# GPU/compute instance layout does not, hence the boot unit.
#
#   MIG_LAYOUT="1g.18gb*7"                       # seven equal slices (default)
#   MIG_LAYOUT="3g.71gb 2g.35gb 1g.18gb*2"       # mixed sizes
#
# Placement matters for mixed layouts. On an H200, creating 3g.71gb, 2g.35gb,
# 1g.18gb, 1g.18gb in the driver's default positions fails on the last slice
# (3g lands on memory slices 0-3, 2g on 4-5, the first 1g on 6, and no 1g can
# start at 7). `--plan` solves the placement from `nvidia-smi mig -lgipp` and
# creates each instance at an explicit start index instead.
#
#   mig-setup.sh            apply the configured layout
#   mig-setup.sh --plan     print the -cgi argument for it and exit (read-only)
set -uo pipefail

LAYOUT_FILE="${MIG_LAYOUT_FILE:-/etc/default/nvidia-mig-layout}"
[ -f "$LAYOUT_FILE" ] && . "$LAYOUT_FILE"
MIG_LAYOUT="${MIG_LAYOUT:-1g.18gb*7}"

command -v nvidia-smi >/dev/null || { echo "nvidia-smi absent"; exit 1; }

# At boot this may run before the driver/device nodes are ready. Wait, don't fail.
for i in $(seq 1 60); do
    nvidia-smi -L >/dev/null 2>&1 && break
    [ "$i" -eq 60 ] && { echo "driver not ready after 120s"; exit 1; }
    sleep 2
done

# Expand "1g.18gb*2 3g.71gb" -> one profile name per line, largest first.
want_list() {
    local tok name n
    for tok in $(echo "$MIG_LAYOUT" | tr ',' ' '); do
        name="${tok%\**}"; n=1
        [ "$tok" != "$name" ] && n="${tok#*\*}"
        for _ in $(seq 1 "$n"); do echo "$name"; done
    done | sort -t g -k1,1nr
}

# Solve placements: prints e.g. "9:4,14:0,19:2,19:3" or fails.
plan() {
    want_list | python3 -c '
import re, subprocess, sys
want = [l.strip() for l in sys.stdin if l.strip()]
lgip = subprocess.run(["nvidia-smi", "mig", "-i", "0", "-lgip"], capture_output=True, text=True).stdout
ids = {m.group(1): int(m.group(2)) for m in re.finditer(r"MIG\s+(\S+)\s+(\d+)\s+\d+/\d+", lgip)}
lgipp = subprocess.run(["nvidia-smi", "mig", "-i", "0", "-lgipp"], capture_output=True, text=True).stdout
places = {}
for m in re.finditer(r"Profile ID\s+(\d+) Placements?\s*:\s*\{([\d,]+)\}:(\d+)", lgipp):
    places[int(m.group(1))] = ([int(x) for x in m.group(2).split(",")], int(m.group(3)))
unknown = [w for w in want if w not in ids]
if unknown:
    sys.exit("unknown MIG profile(s): %s. This GPU offers: %s" % (", ".join(unknown), ", ".join(sorted(ids))))
slots = 1 + max(s + n - 1 for st, n in places.values() for s in st)
def solve(i, used, out):
    if i == len(want):
        return out
    pid = ids[want[i]]
    starts, size = places[pid]
    for s in starts:
        span = set(range(s, s + size))
        if not span & used:
            r = solve(i + 1, used | span, out + ["%d:%d" % (pid, s)])
            if r:
                return r
    return None
r = solve(0, set(), [])
if not r:
    sys.exit("layout does not fit on this GPU: %s" % " ".join(want))
print(",".join(r))
'
}

if [ "${1:-}" = "--plan" ]; then
    plan
    exit $?
fi

# Ensure MIG mode is on (persists across reboot, but re-assert defensively).
if ! nvidia-smi -i 0 --query-gpu=mig.mode.current --format=csv,noheader | grep -q Enabled; then
    echo "MIG mode off; enabling"
    nvidia-smi -i 0 -mig 1 || exit 1
fi

WANT_SORTED=$(want_list | sort)
HAVE_SORTED=$(nvidia-smi -L 2>/dev/null | grep -o 'MIG [0-9a-z.+]*' | awk '{print $2}' | sort)
if [ "$WANT_SORTED" = "$HAVE_SORTED" ]; then
    echo "MIG layout already correct: $(echo $WANT_SORTED)"
    exit 0
fi

CGI=$(plan) || exit 1
echo "Rebuilding MIG layout -> $(echo $WANT_SORTED) (placements $CGI)"
nvidia-smi mig -dci  || true      # destroy compute instances first
nvidia-smi mig -dgi  || true      # then GPU instances
nvidia-smi mig -i 0 -cgi "$CGI" -C || { echo "MIG creation FAILED"; exit 1; }

HAVE_SORTED=$(nvidia-smi -L | grep -o 'MIG [0-9a-z.+]*' | awk '{print $2}' | sort)
[ "$WANT_SORTED" = "$HAVE_SORTED" ] || { echo "expected: $(echo $WANT_SORTED); got: $(echo $HAVE_SORTED)"; exit 1; }
echo "MIG layout OK: $(echo $HAVE_SORTED)"
