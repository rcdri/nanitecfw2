SECTIONS
{
    . = 0x030512BC;
    .text : { *(.text) *(.text.[!h]*) *(.rodata*) }
    ASSERT(. <= 0x03051550, "turbo: main code does not fit the LRC cave")
    . = 0x030518F0;
    .hi : { *(.text.hi) }
    ASSERT(. <= 0x0305199A, "turbo: hooks do not fit the cave after nanitec")
    /DISCARD/ : { *(.data*) *(.bss*) *(.ARM.exidx*) *(.comment) *(.note*) *(.ARM.attributes) }
}
