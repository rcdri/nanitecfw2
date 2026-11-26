/* libopus build configuration for the ECHO NANO codec core (Cortex-M3, no FPU, no DSP extension) */
#define OPUS_BUILD 1
#define FIXED_POINT 1
#define DISABLE_FLOAT_API 1
#define NONTHREADSAFE_PSEUDOSTACK 1          /* one 16 KB scratch stack from the arena (peak use 13.9 KB) */
#define GLOBAL_STACK_SIZE 16384
#define OVERRIDE_celt_fatal 1
#define OPUS_ARM_MAY_HAVE_EDSP 0
/* SMULL-based 16x32 / 32x32 multiplies and FFT butterflies (celt/arm/fixed_armv4.h,
   kiss_fft_armv4.h): valid Thumb-2 on the M3, -22% instructions vs the generic C macros */
#define OPUS_ARM_INLINE_ASM 1
