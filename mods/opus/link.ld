ENTRY(ogg_entry)
SECTIONS
{
    . = 0x0000F5D8;                      /* end of the stock Vorbis image code (core-B view) */
    .text : { KEEP(*(.text.ogg_entry)) *(.text .text.*) *(.rodata .rodata.*) }
    __code_end__ = .;
    ASSERT(. <= 0x0001FF00, "opus code does not fit below the data segment (0x20000)")
    .data : { *(.data .data.*) }
    ASSERT(SIZEOF(.data) == 0, "no initialised data allowed (not loaded)")
    . = 0x0105F000;                      /* core-B RAM image 77 never uses */
    .bss (NOLOAD) : { __bss_start__ = .; *(.bss .bss.* COMMON) __bss_end__ = .; }
    ASSERT(. <= 0x01064000, "bss too large")
    /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.note*) *(.ARM.attributes) }
}
