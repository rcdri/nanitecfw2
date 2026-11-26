/*
 * turbo: sector cache for the media-library refresh (SNOWSKY ECHO NANO V2.0.0).
 *
 * The stock refresh reads the SD card one 512-byte sector per call, and the
 * tag parsers ask for the same sector again for almost every field they read
 * (about 90 % of SD reads during a scan repeat the previous one). The sort
 * then re-reads the same database sectors on the internal flash many times.
 *
 * card_read (mod00 0x03061596, internal flash) and the slot-0 read wrapper
 * (0x030616ea, SD card) are redirected here. While a refresh is running
 * (armed by the refresh driver, and only while window 0x0d "updating media
 * library" is on screen) single-sector reads go through a 28-entry LRU cache
 * kept in RAM no module uses (0x0309c000..0x0309fa00). Both card-write
 * wrappers invalidate overlapping entries first, so the cache never returns
 * data older than the card. Outside a refresh everything goes straight to the
 * driver, as in stock.
 *
 * Code lives over an unreferenced LRC debug dump in mod00 (0x030512bc..), the
 * trampolines and refresh hooks in the dead code after nanitec (0x030518f0..).
 */
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned char u8;

#define FW(addr, ret, ...) ((ret (*)(__VA_ARGS__))((addr) | 1))
#define fw_memcpy   FW(0x030579ba, void, void *dst, const void *src, u32 n)
#define fw_progress FW(0x0307a5f4, void, u32 step)          /* mod05 */
#define WINDOW      (*(volatile u8 *)0x03004a74)
#define WIN_REFRESH 0x0d

#define NENT 28
struct tag { u32 lba; u32 stamp; u8 slot, valid, _pad[2]; };
struct state { u32 magic, active, clock, hits, misses; struct tag t[NENT]; };
#define ST   ((struct state *)0x0309c000)
#define DATA ((u8 *)0x0309c200)                              /* NENT * 512 -> 0x0309fa00 */
#define MAGIC 0x54524230

int orig_read(u32 slot, u32 lba, u32 n, void *buf);
int orig_read0(u32 x, u32 lba, u32 n, void *buf);
int orig_write(u32 slot, u32 lba, u32 n, const void *buf);
int orig_write0(u32 x, u32 lba, u32 n, const void *buf);

static void invalidate(u32 slot, u32 lba, u32 n)
{
    struct state *s = ST;
    if (s->magic != MAGIC)
        return;
    for (int i = 0; i < NENT; i++)
        if (s->t[i].valid && s->t[i].slot == slot && s->t[i].lba - lba < n)
            s->t[i].valid = 0;
}

static void reset(u32 active)
{
    struct state *s = ST;
    s->magic = MAGIC;
    s->active = active;
    s->clock = s->hits = s->misses = 0;
    for (int i = 0; i < NENT; i++)
        s->t[i].valid = 0;
}

static int dev_read(u32 slot, u32 x, u32 lba, u32 n, u8 *buf)
{
    return slot == 0x80 ? orig_read0(x, lba, n, buf) : orig_read(slot, lba, n, buf);
}

/* slot: card slot for card_read, 0x80 for the slot-0 read wrapper (key slot 0) */
static int cached(u32 slot, u32 x, u32 lba, u32 n, u8 *buf)
{
    struct state *s = ST;
    if (n != 1 || s->magic != MAGIC || !s->active || WINDOW != WIN_REFRESH)
        return dev_read(slot, x, lba, n, buf);
    u32 key = slot & 0x7f;
    int victim = 0;
    u32 oldest = 0xffffffff;
    for (int i = 0; i < NENT; i++) {
        struct tag *t = &s->t[i];
        if (t->valid && t->lba == lba && t->slot == key) {
            t->stamp = ++s->clock;
            s->hits++;
            fw_memcpy(buf, DATA + i * 512, 512);
            return 0;
        }
        u32 age = t->valid ? t->stamp : 0;
        if (age < oldest) {
            oldest = age;
            victim = i;
        }
    }
    int r = dev_read(slot, x, lba, 1, buf);
    s->misses++;
    if (r == 0) {
        struct tag *t = &s->t[victim];
        fw_memcpy(DATA + victim * 512, buf, 512);
        t->lba = lba;
        t->slot = key;
        t->stamp = ++s->clock;
        t->valid = 1;
    }
    return r;
}

__attribute__((used)) int cache_read(u32 slot, u32 lba, u32 n, u8 *buf)
{
    return cached(slot, 0, lba, n, buf);
}

__attribute__((used)) int cache_read0(u32 x, u32 lba, u32 n, u8 *buf)
{
    return cached(0x80, x, lba, n, buf);
}

__attribute__((used)) int cache_write(u32 slot, u32 lba, u32 n, const void *buf)
{
    invalidate(slot, lba, n);
    return orig_write(slot, lba, n, buf);
}

__attribute__((used)) int cache_write0(u32 x, u32 lba, u32 n, const void *buf)
{
    invalidate(0, lba, n);
    return orig_write0(x, lba, n, buf);
}

/* mod05 refresh driver: first and last progress() calls */
__attribute__((used, section(".text.hi"))) void cache_begin(u32 step)
{
    reset(1);
    fw_progress(step);
}

__attribute__((used, section(".text.hi"))) void cache_end(u32 step)
{
    ST->active = 0;
    fw_progress(step);
}

/* displaced first instructions of the hooked functions, then back into them */
__attribute__((naked, used, section(".text.hi"))) int orig_read(u32 slot, u32 lba, u32 n, void *buf)
{
    __asm__ volatile("push {r4, r5, r6, lr}\n mov r5, r1\n ldr ip, =0x0306159b\n bx ip\n .ltorg");
}

__attribute__((naked, used, section(".text.hi"))) int orig_read0(u32 x, u32 lba, u32 n, void *buf)
{
    __asm__ volatile("push {r4, r5, r6, lr}\n movs r5, #0\n ldr ip, =0x030616ef\n bx ip\n .ltorg");
}

__attribute__((naked, used, section(".text.hi"))) int orig_write(u32 slot, u32 lba, u32 n, const void *buf)
{
    __asm__ volatile("push {r4, r5, r6, lr}\n mov r5, r1\n ldr ip, =0x030630c9\n bx ip\n .ltorg");
}

__attribute__((naked, used, section(".text.hi"))) int orig_write0(u32 x, u32 lba, u32 n, const void *buf)
{
    __asm__ volatile("push {r4, r5, r6, lr}\n movs r5, #0\n ldr ip, =0x03063119\n bx ip\n .ltorg");
}
