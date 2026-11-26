#!/bin/sh
# Build the modded images from FiiO's official V2.0.0 NANOV200.IMG.
#
#   ./build.sh NANOV200.IMG [OUT_DIR]          (default OUT_DIR: out)
#
#   OUT_DIR/NANOV200_mod.IMG        navilib + nanitec + turbo + opus          dated $DATE  (20261031)
#   OUT_DIR/NANOV200_mod_power.IMG  the same with nanitec --power             dated $DATE
#   OUT_DIR/NANOV200_recovery.IMG   unmodified V2.0.0 code                    dated $RDATE (20261031)
#
# Dates: see "Install" in README.md. The updater doesn't compare them; these are the known-good ones.
set -e
IN=${1:?usage: ./build.sh NANOV200.IMG [OUT_DIR]}
OUT=${2:-out}
DATE=${DATE:-20261031}
RDATE=${RDATE:-20261031}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-python3}
STOCK_SHA256=e60ddde7ceae9c83f95db7710f4633a31cf72295d4821128274718dec16ff993

sha() { "$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }

if [ "$(sha "$IN")" != "$STOCK_SHA256" ]; then
    echo "$IN is not the official V2.0.0 NANOV200.IMG (SHA-256 $STOCK_SHA256)." >&2
    echo "The patch scripts only know that build; set FORCE=1 to try anyway." >&2
    [ -n "$FORCE" ] || exit 1
fi

mkdir -p "$OUT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
M="$HERE/mods"

"$PY" "$M/navilib.py" "$IN" "$TMP/navilib.IMG"
"$PY" "$M/nanitec.py" "$TMP/navilib.IMG" "$TMP/nanitec.IMG"
"$PY" "$M/turbo.py"   "$TMP/nanitec.IMG" "$TMP/turbo.IMG"
"$PY" "$M/opus.py"    "$TMP/turbo.IMG" "$OUT/NANOV200_mod.IMG" --date "$DATE"

"$PY" "$M/nanitec.py" "$TMP/navilib.IMG" "$TMP/nanitec_p.IMG" --power
"$PY" "$M/turbo.py"   "$TMP/nanitec_p.IMG" "$TMP/turbo_p.IMG"
"$PY" "$M/opus.py"    "$TMP/turbo_p.IMG" "$OUT/NANOV200_mod_power.IMG" --date "$DATE"

"$PY" "$M/nanitec.py" --recovery "$IN" "$OUT/NANOV200_recovery.IMG" --date "$RDATE"

# every byte the mods overwrite must be unused by live stock code (needs capstone)
if "$PY" -c 'import capstone' 2>/dev/null; then
    for f in "$OUT"/NANOV200_mod.IMG "$OUT"/NANOV200_mod_power.IMG; do
        if ! "$PY" "$HERE/tools/cavecheck.py" "$IN" "$f" > "$TMP/cavecheck.txt"; then
            cat "$TMP/cavecheck.txt" >&2
            echo "cavecheck: $f overwrites bytes that live code still uses" >&2
            exit 1
        fi
    done
    echo "cavecheck: no live references to overwritten bytes"
else
    echo "(pip install capstone to verify the patch caves with tools/cavecheck.py)"
fi

echo
for f in "$OUT"/NANOV200_mod.IMG "$OUT"/NANOV200_mod_power.IMG "$OUT"/NANOV200_recovery.IMG; do
    echo "$(sha "$f")  $f"
done
echo "Flash one of them renamed to NANOV200.IMG (see README.md), then run Update media library once."
