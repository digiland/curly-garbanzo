#!/usr/bin/env bash
# Per-GPU MIG layout. The upstream host/mig-setup.sh drives GPU 0 only and its
# "already correct?" check greps MIG devices across *every* GPU, so on a box with
# MIG on more than one GPU it would see a mismatch and re-slice GPU 0 while
# leaving the others alone -- or, worse, `mig -dci`/`-dgi` without `-i` wipes the
# other GPUs' instances too.
#
# This box has MIG on GPU 0 and GPU 2, and leaves GPU 1 and GPU 3 whole. Name
# each MIG GPU by index in /etc/default/nvidia-mig-layout:
#
#   MIG_LAYOUT_0="1g.18gb*7"      # seven 18 GB slices
#   MIG_LAYOUT_2="2g.35gb*3"      # three 35 GB slices
#
# A GPU with no MIG_LAYOUT_<n> is not touched at all (MIG mode is left as-is).
# Idempotent: does nothing when a GPU already has the requested instances.
#
#   mig-setup-multi.sh           apply every configured layout
#   mig-setup-multi.sh --plan    print the -cgi argument per GPU, change nothing
set -uo pipefail

LAYOUT_FILE="${MIG_LAYOUT_FILE:-/etc/default/nvidia-mig-layout}"
[ -f "$LAYOUT_FILE" ] && . "$LAYOUT_FILE"

MODE="${1:-apply}"
case "$MODE" in
    apply|--plan) ;;
    *) echo "usage: $0 [--plan]"; exit 1 ;;
esac

command -v nvidia-smi >/dev/null || { echo "nvidia-smi absent"; exit 1; }

# `nvidia-smi -i N -L` is not a valid option, and reading one GPU's instances
# (as opposed to the global `nvidia-smi -L`) needs root. Refuse early rather
# than read an empty list and "rebuild" a layout that is already correct.
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

# Instance profile names on one GPU, e.g. "1g.18gb 1g.18gb".
have_list() {
    nvidia-smi mig -i "$1" -lgi 2>/dev/null \
        | grep -o 'MIG [0-9a-z.+]*' | awk '{print $2}' | sort | tr '\n' ' '
}

# At boot this may run before the driver/device nodes are ready. Wait, don't fail.
for i in $(seq 1 60); do
    nvidia-smi -L >/dev/null 2>&1 && break
    [ "$i" -eq 60 ] && { echo "driver not ready after 120s"; exit 1; }
    sleep 2
done

# Expand "1g.18gb*2 3g.71gb" -> one profile name per line.
want_list() {
    local layout="$1" tok name n
    for tok in $(echo "$layout" | tr ',' ' '); do
        name="${tok%\**}"; n=1
        [ "$tok" != "$name" ] && n="${tok#*\*}"
        for _ in $(seq 1 "$n"); do echo "$name"; done
    done
}

# Solve placements for one GPU: prints e.g. "9:4,14:0,19:2,19:3" or fails.
plan() {
    local gpu="$1" layout="$2"
    want_list "$layout" | python3 -c '
import re, subprocess, sys
gpu = sys.argv[1]
want = [l.strip() for l in sys.stdin if l.strip()]
lgip = subprocess.run(["nvidia-smi", "mig", "-i", gpu, "-lgip"], capture_output=True, text=True).stdout
ids = {m.group(1): int(m.group(2)) for m in re.finditer(r"MIG\s+(\S+)\s+(\d+)\s+\d+/\d+", lgip)}
lgipp = subprocess.run(["nvidia-smi", "mig", "-i", gpu, "-lgipp"], capture_output=True, text=True).stdout
places = {}
for m in re.finditer(r"Profile ID\s+(\d+) Placements?\s*:\s*\{([\d,]+)\}:(\d+)", lgipp):
    places[int(m.group(1))] = ([int(x) for x in m.group(2).split(",")], int(m.group(3)))
unknown = [w for w in want if w not in ids]
if unknown:
    sys.exit("GPU %s: unknown MIG profile(s): %s. This GPU offers: %s"
             % (gpu, ", ".join(unknown), ", ".join(sorted(ids))))
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
    sys.exit("GPU %s: layout does not fit: %s" % (gpu, " ".join(want)))
print(",".join(r))
' "$gpu"
}

apply_gpu() {
    local gpu="$1" layout="$2" want have cgi
    want=$(want_list "$layout" | sort | tr '\n' ' ')
    have=$(have_list "$gpu")

    if [ "$want" = "$have" ]; then
        echo "GPU $gpu: layout already correct ($want)"
        return 0
    fi

    cgi=$(plan "$gpu" "$layout") || return 1

    if [ "$MODE" = "--plan" ]; then
        echo "GPU $gpu: want [$want] have [$have] placements $cgi"
        return 0
    fi

    if ! nvidia-smi -i "$gpu" --query-gpu=mig.mode.current --format=csv,noheader | grep -q Enabled; then
        echo "GPU $gpu: MIG mode off; enabling"
        nvidia-smi -i "$gpu" -mig 1 || return 1
    fi

    echo "GPU $gpu: rebuilding -> $want (placements $cgi)"
    nvidia-smi mig -i "$gpu" -dci 2>/dev/null || true
    nvidia-smi mig -i "$gpu" -dgi 2>/dev/null || true
    nvidia-smi mig -i "$gpu" -cgi "$cgi" -C || { echo "GPU $gpu: MIG creation FAILED"; return 1; }

    have=$(have_list "$gpu")
    [ "$want" = "$have" ] || { echo "GPU $gpu: expected [$want]; got [$have]"; return 1; }
    echo "GPU $gpu: layout OK ($want)"
}

rc=0
found=0
for gpu in $(nvidia-smi --query-gpu=index --format=csv,noheader); do
    var="MIG_LAYOUT_$gpu"
    layout="${!var:-}"
    [ -n "$layout" ] || continue
    found=1
    apply_gpu "$gpu" "$layout" || rc=1
done

if [ "$found" -eq 0 ]; then
    echo "no MIG_LAYOUT_<n> entries in $LAYOUT_FILE; nothing to do"
fi

[ "$MODE" = "--plan" ] && echo "(dry run: nothing changed)"
exit "$rc"
