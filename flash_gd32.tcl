# Manual flash programmer for GD32F130K6.
# Bypasses DBGMCU auto-probe by driving the flash controller directly.
# Uses xxd to convert binary to hex text, avoiding Jim Tcl null-byte bugs
# in string index on binary data.
# Usage: sourced by OpenOCD via -c "source flash_gd32.tcl; gd32_flash <file.bin>"

proc gd32_wait_bsy {} {
    while {[mrw 0x4002200C] & 0x01} {}
    mww 0x4002200C 0x00000020
}

proc gd32_flash {filename} {
    # Convert binary to continuous hex string via xxd to avoid
    # Jim Tcl's null-byte truncation in string index.
    set hexdata [exec xxd -p $filename]
    set hexdata [string map {"\n" "" " " ""} $hexdata]

    set len [expr {[string length $hexdata] / 2}]
    if {$len % 2} { append hexdata "ff"; incr len }
    echo "==> $filename : $len bytes"

    # Unlock flash
    mww 0x40022004 0x45670123
    mww 0x40022004 0xCDEF89AB
    set cr [mrw 0x40022010]
    if {$cr & 0x80} { echo "ERROR: unlock failed (CR=0x[format %08x $cr])"; return }

    # Erase pages (1 KB each)
    set npages [expr {($len + 1023) / 1024}]
    echo "==> Erasing $npages pages..."
    for {set p 0} {$p < $npages} {incr p} {
        set pa [expr {0x08000000 + $p * 1024}]
        mww 0x40022010 0x00000002
        mww 0x40022014 $pa
        mww 0x40022010 0x00000042
        gd32_wait_bsy
        echo "    page $p erased (0x[format %08x $pa])"
    }

    # Program halfword by halfword from hex string.
    # hex_idx advances 2 chars per byte, 4 chars per halfword.
    echo "==> Programming..."
    mww 0x40022010 0x00000001
    for {set i 0} {$i < $len} {incr i 2} {
        set hx [expr {$i * 2}]
        set lo_hex [string range $hexdata $hx           [expr {$hx + 1}]]
        set hi_hex [string range $hexdata [expr {$hx + 2}] [expr {$hx + 3}]]
        scan $lo_hex %x lo
        scan $hi_hex %x hi
        set hw [expr {($hi << 8) | $lo}]
        mwh [expr {0x08000000 + $i}] $hw
    }
    gd32_wait_bsy

    # Lock and reset
    mww 0x40022010 0x00000080
    echo "==> Flash complete. Resetting..."
    reset run
    echo "==> Done!"
}
