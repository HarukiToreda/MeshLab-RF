/* nRF52840 + S140 v6 memory layout for the standalone survey firmware.
 *
 * 0x26000..0x4CFFF  application (156 KiB maximum)
 * 0x4D000..0xECFFF  retained survey log (640 KiB)
 * 0xED000..          bootloader-reserved area
 *
 * Keeping the log outside the FLASH linker region makes firmware growth fail
 * at link time instead of silently overwriting measurements.
 */

SEARCH_DIR(.)
GROUP(-lgcc -lc -lnosys)

MEMORY
{
  FLASH (rx) : ORIGIN = 0x26000, LENGTH = 0x4D000 - 0x26000
  RAM (rwx)  : ORIGIN = 0x20006000, LENGTH = 0x20040000 - 0x20006000
}

SECTIONS
{
  . = ALIGN(4);
  .svc_data :
  {
    PROVIDE(__start_svc_data = .);
    KEEP(*(.svc_data))
    PROVIDE(__stop_svc_data = .);
  } > RAM

  .fs_data :
  {
    PROVIDE(__start_fs_data = .);
    KEEP(*(.fs_data))
    PROVIDE(__stop_fs_data = .);
  } > RAM
} INSERT AFTER .data;

INCLUDE "nrf52_common.ld"
