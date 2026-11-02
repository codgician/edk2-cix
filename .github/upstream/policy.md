# Firmware hardening policy

This fork builds the Radxa Orion O6 (CIX Sky1) UEFI firmware with a firmware
TPM, measured boot and related hardening. Upstream changes are welcome only when
they keep every invariant below. Reviews, triage and pull requests are judged
against this list; a change that weakens one needs an explicit, documented
decision by the owner.

## Measured boot and the TPM

1. **PCR0 measures the firmware.** `FirmwareMeasureDxe` extends PCR0 with the
   extracted FVMAIN before the ReadyToBoot separator. The NUR package predicts
   the resulting PCR0 (`share/edk2/radxa/orion-o6/pcr0`); a healthy boot must
   reproduce it. Changes that alter what is measured, the order of the
   measurement and the separator, or the event types need an explanation.
2. **Measured boot stays on.** `Tcg2Dxe` performs measured boot through the
   `FTPMDxe` transport to the OP-TEE firmware TPM. Do not disable, stub or
   reorder these drivers, and do not change their DEPEX without a reason.
3. **TPM boot gate.** `PlatformBootManagerLib` refuses to boot when the TPM is
   not usable. It must not be bypassed, made optional by default or skipped on
   error paths.
4. **Physical presence.** TPM operations that need physical presence (clear,
   hierarchy changes) require confirmation with F12 in the firmware. The vendor
   `Tcg2Config` page stays. No path may confirm PP requests without a key press.

## Variables and flash

5. **Setup variables are NV|BS only.** Platform setup variables must never gain
   `EFI_VARIABLE_RUNTIME_ACCESS`, so the OS cannot read or change them.
6. **No new SPI flash writers.** Do not add drivers, protocols, runtime services
   or applications that can write the SPI flash, beyond what the vendor image
   already contains. The raw `FlashUpdate` recovery path stays.

## Platform policy

7. **SMMU selection.** Keep the vendor SMMU/no-SMMU IORT selection
   (`ConfigureIortForSmmu`) and ACPI boot as the default boot description.
8. **FV capacity.** FVMAIN_COMPACT is fixed at `0x1f0000` with about 41 KiB
   free. The FD must not grow; vendor changes that enlarge the image (for
   example "increase FvMain size" or "increase bootloader3 size") are a
   conflict to resolve, not a change to take as is.

## Provenance

9. **Licences.** `Neol00/edk2-cix-unlocked` is GPL-3.0 at its root. It is an
   idea source only: never copy its code. Reimplement from specifications,
   upstream tianocore, or our own reasoning.
10. **Upstream picks.** tianocore fixes are taken with `git cherry-pick -x`,
    keeping the original subject, so later triage recognises them.
