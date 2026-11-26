/*
 * Opus (CELT) playback for the SNOWSKY ECHO NANO V2.0.0 codec core.
 *
 * Linked into the Ogg Vorbis codec image (segment 77). The image's codec table
 * slot for Ogg (type 4) is pointed at ogg_entry(): on OPEN it looks at the first
 * Ogg page; "OpusHead" streams are handled here, everything else goes to the
 * stock Tremor code at 0x29d0 unchanged.
 *
 * Rockchip codec interface: CodecFunction(subfn, p1, p2, p3)
 *   3 bitrate  4 sample rate  5 channels  6 length (ms)  7 time (ms)  8 open
 *   10 output buffer (stock code)  12 decode 512 stereo s16 frames  14 seek (ms)
 *   15 close  17 bits per sample (stock code)
 *
 * Only CELT-mode packets (TOC configs 16-31) are decoded: that is what libopus
 * produces for music at >= ~64 kbit/s stereo. Files that are mostly SILK/hybrid
 * (speech, low bitrates) are refused at open; a stray SILK/hybrid packet in a
 * CELT file plays as silence.
 * All state lives in core-B RAM that image 77 never touches (0x0105F000..).
 */
#include <stddef.h>
#include <stdint.h>
#include "celt.h"
#include "opus.h"

typedef int (*codec_fn)(int, intptr_t, intptr_t, intptr_t);
#define STOCK_OGG     ((codec_fn)0x000029d1)            /* Tremor-based Ogg Vorbis codec */

/* runtime (RKFIO-style) file functions, table at 0x010220e4 */
#define FIO           ((volatile uint32_t *)0x010220e4)
#define F_HANDLE      (*(volatile int *)0x01022108)
static int f_read(void *buf, int n)   { return ((int (*)(void *, int, int))FIO[1])(buf, n, F_HANDLE); }
static int f_seek(int off)            { return ((int (*)(int, int, int))FIO[2])(off, 0, F_HANDLE); }
static int f_length(void)             { return ((int (*)(int))FIO[5])(F_HANDLE); }

/* PCM output double buffer shared with the stock code (cmd 10 hands it out) */
#define OUT_STATE     ((volatile int *)0x01029888)      /* +4 idx, +8 fill, +0xc handle */
#define OUT_BUFS      ((int16_t * volatile *)0x01029898)
#define OUT_OFF       (*(volatile int *)0x01022104)
#define ARENA_END     ((char *)0x0106FC00)
extern char __bss_start__[], __bss_end__[];
#define OUT_FRAMES    512

/* ---------------------------------------------------------------- memory */
static char *arena;

void *malloc(size_t n)
{
    char *p = (char *)(((uintptr_t)arena + 7) & ~7u);
    if (p + n > ARENA_END)
        return 0;
    arena = p + n;
    return p;
}
void *calloc(size_t a, size_t b) { char *p = malloc(a * b); if (p) for (size_t i = 0; i < a * b; i++) p[i] = 0; return p; }
void *realloc(void *p, size_t n) { (void)p; (void)n; return 0; }
void free(void *p) { (void)p; }

/* CELT moves its history buffers (int32, int16) every frame: copy in words
   or halfwords whenever the alignment allows it */
__attribute__((no_builtin)) void *memcpy(void *d, const void *s, size_t n)
{
    uintptr_t al = (uintptr_t)d | (uintptr_t)s | n;
    if (!(al & 3)) {
        uint32_t *x = d; const uint32_t *y = s;
        for (n >>= 2; n >= 4; n -= 4, x += 4, y += 4) {
            uint32_t t0 = y[0], t1 = y[1], t2 = y[2], t3 = y[3];
            x[0] = t0; x[1] = t1; x[2] = t2; x[3] = t3;
        }
        while (n--) *x++ = *y++;
    } else if (!(al & 1)) {
        uint16_t *x = d; const uint16_t *y = s;
        for (n >>= 1; n; n--) *x++ = *y++;
    } else {
        char *x = d; const char *y = s;
        while (n--) *x++ = *y++;
    }
    return d;
}
__attribute__((no_builtin)) void *memmove(void *d, const void *s, size_t n)
{
    uintptr_t al = (uintptr_t)d | (uintptr_t)s | n;
    if ((char *)d <= (const char *)s || (char *)d >= (const char *)s + n)
        return memcpy(d, s, n);              /* memcpy copies upwards */
    if (!(al & 3)) {
        uint32_t *x = (uint32_t *)((char *)d + n); const uint32_t *y = (const uint32_t *)((const char *)s + n);
        for (n >>= 2; n; n--) *--x = *--y;
    } else if (!(al & 1)) {
        uint16_t *x = (uint16_t *)((char *)d + n); const uint16_t *y = (const uint16_t *)((const char *)s + n);
        for (n >>= 1; n; n--) *--x = *--y;
    } else {
        char *x = (char *)d + n; const char *y = (const char *)s + n;
        while (n--) *--x = *--y;
    }
    return d;
}
__attribute__((no_builtin)) void *memset(void *d, int c, size_t n)
{
    char *a = d;
    while (n && ((uintptr_t)a & 3)) { *a++ = (char)c; n--; }
    uint32_t v = (uint8_t)c * 0x01010101u, *w = (uint32_t *)a;
    for (; n >= 4; n -= 4) *w++ = v;
    a = (char *)w;
    while (n--) *a++ = (char)c;
    return d;
}
int memcmp(const void *x, const void *y, size_t n)
{
    const unsigned char *a = x, *b = y;
    for (; n; n--, a++, b++) if (*a != *b) return *a - *b;
    return 0;
}
void celt_fatal(const char *str, const char *file, int line) { (void)str; (void)file; (void)line; for (;;); }

/* ---------------------------------------------------------------- Ogg */
#define RBUF 4096
#define MAXPKT 8192                 /* 120 ms at 510 kbit/s is ~7.7 KB */
struct ogg {
    uint8_t buf[RBUF];
    int pos, len;                 /* read buffer window */
    int filepos;                  /* file offset of buf[len] (next byte to read from file) */
    uint32_t serial;
    uint8_t lacing[255];
    int nseg, seg;                /* current page lacing table and next segment */
    int64_t granule;              /* granule of the current page */
    int page_has_packet_end;
    uint8_t pkt[MAXPKT];
    int pktlen;
};

struct opus_state {
    struct ogg og;
    CELTDecoder *dec;
    int channels, preskip, gain_q8;
    int datastart;                /* first audio page offset */
    int filelen;
    int64_t total;                /* total 48 kHz samples (after pre-skip) */
    int64_t pos;                  /* samples output so far (after pre-skip) */
    int64_t skip;                 /* samples still to drop (pre-skip / seek pre-roll) */
    /* current packet (frames point into og.pkt), decoded one frame at a time
       so a 120 ms packet doesn't need a 120 ms buffer */
    const unsigned char *frame[48];
    opus_int16 frame_len[48];
    int nframes, frame_i, fs, celt, bw, stereo;
    int16_t pcm[2880 * 2];        /* one decoded frame, stereo (up to 60 ms) */
    int pcm_n, pcm_pos;           /* in frames */
    int eof;
};
static struct opus_state *S;
#define OPUS_MAGIC 0x4F505553u        /* RAM outside the image is not cleared at load */
static uint32_t mode_opus;

static int ogg_fill(struct ogg *o)
{
    if (o->pos < o->len) {
        memmove(o->buf, o->buf + o->pos, o->len - o->pos);
        o->len -= o->pos;
    } else
        o->len = 0;
    o->pos = 0;
    int n = f_read(o->buf + o->len, RBUF - o->len);
    if (n <= 0)
        return 0;
    o->len += n;
    o->filepos += n;
    return n;
}

static void ogg_reset(struct ogg *o, int off)
{
    f_seek(off);
    o->pos = o->len = 0;
    o->filepos = off;
    o->nseg = o->seg = 0;
    o->pktlen = 0;
}

/* next page header (lacing loaded); returns file offset of the page or -1 */
static int ogg_next_page(struct ogg *o, int want_serial)
{
    for (;;) {
        if (o->len - o->pos < 27 && !ogg_fill(o) && o->len - o->pos < 27)
            return -1;
        uint8_t *p = o->buf + o->pos;
        if (p[0] != 'O' || p[1] != 'g' || p[2] != 'g' || p[3] != 'S') {
            o->pos++;
            continue;
        }
        int nseg = p[26];
        if (o->len - o->pos < 27 + nseg) {
            if (!ogg_fill(o)) return -1;
            continue;
        }
        p = o->buf + o->pos;
        uint32_t serial = p[14] | p[15] << 8 | p[16] << 16 | (uint32_t)p[17] << 24;
        int64_t gran = 0;
        for (int i = 7; i >= 0; i--) gran = gran << 8 | p[6 + i];
        int body = 0;
        for (int i = 0; i < nseg; i++) body += p[27 + i];
        int off = o->filepos - o->len + o->pos;
        if (want_serial && serial != o->serial) {
            /* skip whole page */
            int skip = 27 + nseg + body;
            while (o->len - o->pos < skip) { skip -= o->len - o->pos; o->pos = o->len; if (!ogg_fill(o)) return -1; }
            o->pos += skip;
            continue;
        }
        o->serial = serial;
        o->granule = gran;
        o->nseg = nseg;
        o->seg = 0;
        memcpy(o->lacing, p + 27, nseg);
        o->pos += 27 + nseg;
        if (!(p[5] & 1))          /* not a continuation: drop any partial packet */
            o->pktlen = 0;
        return off;
    }
}

/* next complete packet into o->pkt; returns length or -1 at end */
static int ogg_packet(struct ogg *o)
{
    for (;;) {
        while (o->seg < o->nseg) {
            int l = o->lacing[o->seg++];
            while (o->len - o->pos < l) if (!ogg_fill(o)) return -1;
            if (o->pktlen + l <= MAXPKT)
                memcpy(o->pkt + o->pktlen, o->buf + o->pos, l);
            o->pktlen += l;
            o->pos += l;
            if (l < 255) {
                int n = o->pktlen;
                o->pktlen = 0;
                return n;                      /* > MAXPKT: only the start was kept */
            }
        }
        if (ogg_next_page(o, 1) < 0)
            return -1;
    }
}

/* granule of the last page of our stream (scan the tail of the file) */
static int64_t last_granule(struct ogg *o, int filelen)
{
    int64_t best = -1;
    for (int back = 16384; back <= 262144 && best < 0; back *= 4) {
        int start = filelen > back ? filelen - back : 0;
        ogg_reset(o, start);
        for (;;) {
            if (ogg_next_page(o, 1) < 0) break;
            if (o->granule >= 0) best = o->granule;
        }
        if (start == 0) break;
    }
    return best;
}

/* ---------------------------------------------------------------- Opus */
static int toc_frame_size(int toc)        /* CELT configs only */
{
    return 120 << ((toc >> 3) & 3);       /* 2.5, 5, 10, 20 ms at 48 kHz */
}

/* split a packet into frames; returns 0 if it isn't a valid Opus packet */
static int start_packet(const uint8_t *data, int len)
{
    int toc = data[0];
    int n = opus_packet_parse(data, len, NULL, S->frame, S->frame_len, NULL);
    if (n <= 0)
        return 0;
    S->celt = (toc >> 3) >= 16;
    S->fs = S->celt ? toc_frame_size(toc) : opus_packet_get_samples_per_frame(data, 48000);
    S->bw = (toc >> 3) >= 28 ? 21 : (toc >> 3) >= 24 ? 19 : (toc >> 3) >= 20 ? 17 : 13;
    S->stereo = (toc >> 2) & 1;
    S->nframes = n;
    S->frame_i = 0;
    return 1;
}

/* decode the next frame of the current packet into S->pcm */
static void decode_frame(void)
{
    int i = S->frame_i++, fs = S->fs;
    int16_t *out = S->pcm;
    S->pcm_n = fs;
    S->pcm_pos = 0;
    if (S->celt) {
        opus_custom_decoder_ctl(S->dec, CELT_SET_END_BAND(S->bw));
        opus_custom_decoder_ctl(S->dec, CELT_SET_CHANNELS(S->stereo + 1));
        int r = celt_decode_with_ec(S->dec, S->frame[i], S->frame_len[i], out, fs, NULL, 0);
        if (r < 0)
            memset(out, 0, fs * 4);
        else if (S->channels == 1)                     /* decoder runs mono: widen in place */
            for (int k = fs - 1; k >= 0; k--) out[2 * k] = out[2 * k + 1] = out[k];
    } else
        memset(out, 0, fs * 4);                        /* SILK / hybrid: not built in */
    if (S->gain_q8) {
        /* output gain in Q7.8 dB: approximate 10^(g/20) with a Q12 factor */
        int g = S->gain_q8, f = 4096;
        while (g >= 1541) { f = f * 2; g -= 1541; }      /* +6.02 dB */
        while (g <= -1541) { f = f / 2; g += 1541; }
        f = f + (f * g) / 11137;                         /* ~linear between octaves */
        for (int k = 0; k < fs * 2; k++) {
            int v = (out[k] * f) >> 12;
            out[k] = v > 32767 ? 32767 : v < -32768 ? -32768 : v;
        }
    }
}

static int op_open(void)
{
    arena = __bss_end__;
    S = malloc(sizeof *S);
    if (!S) return 0;
    memset(S, 0, sizeof *S);
    struct ogg *o = &S->og;
    S->filelen = f_length();
    ogg_reset(o, 0);
    if (ogg_next_page(o, 0) < 0) return 0;
    int n = ogg_packet(o);
    if (n < 19 || memcmp(o->pkt, "OpusHead", 8)) return 0;
    S->channels = o->pkt[9] ? o->pkt[9] : 1;
    S->preskip = o->pkt[10] | o->pkt[11] << 8;
    S->gain_q8 = (int16_t)(o->pkt[16] | o->pkt[17] << 8);
    if (S->channels > 2 || o->pkt[18] != 0) return 0;      /* mapping family 0 only */
    if (ogg_packet(o) < 0) return 0;                        /* OpusTags */
    S->datastart = o->filepos - o->len + o->pos;
    int64_t g = last_granule(o, S->filelen);
    S->total = g > S->preskip ? g - S->preskip : 0;
    S->dec = malloc(celt_decoder_get_size(S->channels));
    if (!S->dec || celt_decoder_init(S->dec, 48000, S->channels) != OPUS_OK) return 0;
    /* Only CELT is built in. Speech/low-bitrate encodes (SILK or hybrid
       packets) would play as silence, so refuse the file when most of its
       first packets are not CELT: the player then treats it like any other
       file it can't open (and the skip-unsupported mod moves on). */
    ogg_reset(o, S->datastart);
    int celt = 0, other = 0;
    while (celt + other < 32) {
        int n = ogg_packet(o);
        if (n < 0) break;
        if (n == 0 || n > MAXPKT) continue;
        if ((o->pkt[0] >> 3) >= 16) celt++; else other++;
    }
    if (other > celt) return 0;
    /* restart right after the header packets (the audio data starts on a
       fresh page, so no need to re-read possibly huge OpusTags) */
    ogg_reset(o, S->datastart);
    S->skip = S->preskip;
    S->pos = 0;
    return 1;
}

static int fill_pcm(void)
{
    while (S->pcm_pos >= S->pcm_n) {
        if (S->eof) return 0;
        if (S->frame_i >= S->nframes) {
            int n = ogg_packet(&S->og);
            if (n < 0) { S->eof = 1; return 0; }
            if (n == 0 || n > MAXPKT || !start_packet(S->og.pkt, n))
                continue;
        }
        decode_frame();
        if (S->skip > 0) {
            int d = S->skip < S->pcm_n ? (int)S->skip : S->pcm_n;
            S->pcm_pos = d;
            S->skip -= d;
        }
    }
    return 1;
}

static int op_decode(void)
{
    int16_t *dst = OUT_BUFS[OUT_STATE[1]] + OUT_OFF;
    int got = 0;
    while (got < OUT_FRAMES) {
        if (S->total && S->pos >= S->total) break;      /* end trimming */
        if (!fill_pcm()) break;
        int n = S->pcm_n - S->pcm_pos;
        if (n > OUT_FRAMES - got) n = OUT_FRAMES - got;
        if (S->total && S->pos + n > S->total) n = (int)(S->total - S->pos);
        memcpy(dst + got * 2, S->pcm + S->pcm_pos * 2, n * 4);
        S->pcm_pos += n;
        S->pos += n;
        got += n;
    }
    if (!got) return 0;
    if (got < OUT_FRAMES) memset(dst + got * 2, 0, (OUT_FRAMES - got) * 4);
    OUT_STATE[2] = 0;
    return 1;
}

/* seek to ms: bisect on file offset for a page ending before target - 80 ms */
static int op_seek(int ms)
{
    struct ogg *o = &S->og;
    int64_t target = (int64_t)ms * 48;
    if (S->total && target > S->total) target = S->total;
    int64_t want = target + S->preskip - 3840;              /* 80 ms pre-roll */
    int lo = S->datastart, hi = S->filelen, best = -1;
    int64_t bestg = 0;
    if (want > 0) {
        for (int it = 0; it < 24 && hi - lo > 8192; it++) {
            int mid = lo + (hi - lo) / 2;
            ogg_reset(o, mid);
            int off = ogg_next_page(o, 1);
            while (off >= 0 && o->granule < 0) off = ogg_next_page(o, 1);
            if (off < 0 || o->granule > want) hi = mid;
            else { lo = mid; best = off; bestg = o->granule; }
        }
    }
    if (best < 0) {                                          /* from the start */
        ogg_reset(o, S->datastart);
        bestg = 0;
        S->skip = S->preskip + target;
    } else {
        /* start with the first packet that ends after page `best` (granule bestg):
           drop the packets completed on that page, keep a trailing partial one */
        ogg_reset(o, best);
        ogg_next_page(o, 1);
        o->pktlen = 0;
        while (o->seg < o->nseg) {
            int l = o->lacing[o->seg++];
            while (o->len - o->pos < l) if (!ogg_fill(o)) return 0;
            if (o->pktlen + l <= MAXPKT) memcpy(o->pkt + o->pktlen, o->buf + o->pos, l);
            o->pktlen += l;
            o->pos += l;
            if (l < 255) o->pktlen = 0;
        }
        S->skip = target + S->preskip - bestg;
    }
    opus_custom_decoder_ctl(S->dec, OPUS_RESET_STATE);
    S->pcm_n = S->pcm_pos = 0;
    S->nframes = S->frame_i = 0;
    S->eof = 0;
    S->pos = target;
    return 1;
}

static int opus_fn(int cmd, intptr_t p1, intptr_t p2, intptr_t p3)
{
    switch (cmd) {
    case 3: {                                           /* bits/s from file size and length */
        uint32_t ms = (uint32_t)S->total / 48u, bytes = (uint32_t)(S->filelen - S->datastart);
        *(int *)p1 = ms ? (int)((bytes / ms) * 8000u + ((bytes % ms) * 8u * 1000u) / ms) : 0;
        return 1;
    }
    case 4:  *(int *)p1 = 48000; return 1;
    case 5:  *(int *)p1 = S->channels; return 1;
    case 6:  *(int *)p1 = (int)((uint32_t)S->total / 48u); return 1;
    case 7:  *(int *)p1 = (int)((uint32_t)S->pos / 48u); return 1;
    case 12: return op_decode();
    case 14: return op_seek((int)p1);
    case 15: mode_opus = 0; return 1;
    case 10: case 17:
        return STOCK_OGG(cmd, p1, p2, p3);                 /* buffer hand-off, 16 bit */
    default: return 0;
    }
}

/* codec table slot 4 */
__attribute__((used)) int ogg_entry(int cmd, intptr_t p1, intptr_t p2, intptr_t p3)
{
    if (cmd == 8) {
        for (char *p = __bss_start__; p < __bss_end__; p++) *p = 0;
        uint8_t h[64];
        f_seek(0);
        int n = f_read(h, sizeof h);
        int is_opus = 0;
        if (n >= 36 && h[0] == 'O' && h[1] == 'g' && h[2] == 'g' && h[3] == 'S') {
            int body = 27 + h[26];
            if (body + 8 <= n && !memcmp(h + body, "OpusHead", 8)) is_opus = 1;
        }
        f_seek(0);
        if (is_opus) {
            OUT_STATE[0] = 0;
            OUT_STATE[2] = 0;
            OUT_STATE[3] = F_HANDLE;
            int ok = op_open();
            mode_opus = ok ? OPUS_MAGIC : 0;
            return ok;
        }
        mode_opus = 0;
        return STOCK_OGG(cmd, p1, p2, p3);
    }
    if (mode_opus == OPUS_MAGIC)
        return opus_fn(cmd, p1, p2, p3);
    return STOCK_OGG(cmd, p1, p2, p3);
}
