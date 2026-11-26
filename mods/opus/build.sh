#!/bin/sh
# Build the Opus codec blob for image 77. Needs clang + ld.lld (thumbv7m) and the libopus source:
#   git clone https://github.com/xiph/opus mods/opus/libopus
#   git -C mods/opus/libopus checkout fed7c79c09458560452a625fe5b3675593ee7627   # the commit opus.bin was built from
# (or point OPUS_SRC at another checkout)
set -e
HERE=$(cd "$(dirname "$0")" && pwd); SRC=${OPUS_SRC:-$HERE/libopus}; OUT=${1:-$HERE/build}
[ -f "$SRC/celt/celt_decoder.c" ] || { echo "libopus source not found in $SRC (see the top of this script)"; exit 1; }
mkdir -p "$OUT"
CF="--target=thumbv7m-none-eabi -mcpu=cortex-m3 -mthumb -ffreestanding -fno-builtin -nostdlib -ffunction-sections -fdata-sections -fno-common -DHAVE_CONFIG_H -I$HERE/inc -I$HERE -I$SRC/include -I$SRC/celt -I$SRC/silk -I$SRC -w"
# the DSP hot spots (FFT/MDCT, comb filter, band decoding, de-emphasis) get -O2, the rest -Os
HOT=${OPUS_HOT:-"kiss_fft mdct celt"}
for f in bands celt celt_decoder cwrs entcode entdec entenc kiss_fft laplace mathops mdct modes pitch celt_lpc quant_bands rate vq; do
  O=-Os; case " $HOT " in *" $f "*) O=-O2;; esac
  clang $CF $O -c "$SRC/celt/$f.c" -o "$OUT/$f.o"; done
clang $CF -Os -c "$SRC/src/opus.c" -o "$OUT/opus.o"
clang $CF -Os -c "$HERE/opus_codec.c" -o "$OUT/opus_codec.o"
ld.lld -T "$HERE/link.ld" --gc-sections "$OUT"/*.o -o "$OUT/opus.elf"
llvm-objcopy -O binary -j .text "$OUT/opus.elf" "$OUT/opus.bin"
llvm-nm "$OUT/opus.elf" | grep -E " ogg_entry$| __bss_start__$| __bss_end__$| __code_end__$"
ls -l "$OUT/opus.bin"
