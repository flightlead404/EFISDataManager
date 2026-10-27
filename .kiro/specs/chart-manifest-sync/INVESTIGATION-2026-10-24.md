# Chart-sync stall — field investigation 2026-10-24

Live-hardware session on macOS 15.7.9 (Sequoia), HXr card `C2609P2610`
(30.8 GB FAT32, ~9 GB chart payload), SanDisk, connected **direct USB-C via an
A-to-C adapter** (no hub, no dock). Investigating why the chart sync stalls.

## CONFIRMED FACTS (tested this session)

1. **The mount-swap escape genuinely works on the real card.**
   Reproduced the production swap steps by hand against the actual HXr card:
   `diskutil unmount` the FSKit `/Volumes` mount, then
   `sudo /sbin/mount_msdos /dev/disk20s1 <private>`. Result: mounted in **~1 s**,
   and the resulting mount line was `(msdos, local, noowners)` — **NO `fskit`
   tag**. The default auto-mount, by contrast, is `(msdos, ... noatime, fskit)`.
   => `mount_msdos` binds the **in-kernel msdosfs driver**, not FSKit, on this
   OS and this card. The swap's core assumption is VALID. (This directly answers
   "did our remount really use mount_msdos or silently fall back to FSKit?" —
   it really uses the kernel driver.)

2. **The kernel `msdosfs.kext` is present AND loaded** on 15.7.9
   (`kextstat` shows `com.apple.filesystems.msdosfs (1.10)`), alongside the
   FSKit module (`com.apple.fskit.msdos` in pluginkit). Both exist; `mount_msdos`
   uses the kernel one.

3. **The 120 s stall figure is OUR OWN watchdog constant**
   (`drive_updater.STALL_TIMEOUT_SECONDS = 120`), NOT an FSKit/OS timeout. "Stall
   at 120 s" only means our no-progress detector gave up after 2 min; it does
   not implicate FSKit.

4. **Card, USB hardware, adapter, and the FAT32 filesystem are all GOOD.**
   Seattle Avionics' own Windows chart manager, running in Windows 11 under
   Parallels, writes this SAME card reliably every time. Parallels passes the USB
   device through and Windows runs its OWN FAT driver — macOS's FAT code is
   bypassed entirely. So the only failing variable is the macOS write path.

5. **The stall presents as: rsync alive, 0% CPU, 0 bytes/s, 0 progress**
   (per the code's own field-investigation comments in `sync_payload`). On the
   real sync the swap logged `msdos_mount: swapped ...` then `Syncing scanned...`
   then stalled at the 120 s mark; `scanned` tripped the watchdog cleanly, then
   `plates` wedged hard (silent ~27 min, watchdog did NOT re-fire) until the DM
   was quit.

## CORRECTIONS / RETRACTIONS (claims made then disproven this session)

- **"macOS FSKit FAT support is widely reported as bad"** — UNSUBSTANTIATED.
  A web search did not surface clear reports of FSKit FAT32 *write* stalls; only
  adjacent exFAT mount/large-transfer complaints and the general "small-file
  copies are slow on FAT" pattern. Retracted as a stated fact.
- **"120 s is the FSKit signature"** — WRONG; it's our own constant (fact 3).
- **"mount_msdos hangs on the large real card"** — WRONG. The apparent 120 s
  "hang" was a TEST-HARNESS artifact: the script blocked at the interactive
  `sudo` PASSWORD PROMPT, and the shell command-timeout counted that idle prompt
  time. With sudo pre-primed (`sudo -v`), mount_msdos completed in ~1 s (fact 1).
  No real mount hang. (Credit: user caught this.)

## LEADING HYPOTHESIS (NOT yet proven)

The stall is in the **sustained write phase**, on a correctly kernel-mounted
(non-FSKit) card, and is most likely tied to the **rsync many-small-files I/O
profile** against FAT32 over this connection — NOT a mount problem and NOT
FSKit-specific. Supporting: the mount itself is fine (fact 1); Parallels'
*bulk/sequential* writes succeed (fact 4); general reports that small-file FAT
copies are slow. The chart tree is tens of thousands of small plate PNGs —
a metadata-heavy write pattern.

If true, the durable fix is a **bulk/sequential** write that matches what works
under Parallels — i.e. the **image-dd path** (raw block `dd` to the unmounted
slice), which bypasses the FAT driver entirely and never mounts the card. This
is blocked today only by the composed-image-size vs. drive-slice mismatch (see
BACKLOG "Composed-image size vs. drive slice"; default now 25 GiB but needs the
paired Prepare sized-partition change).

## NEXT EXPERIMENTS (to settle the cause, not guess)

1. **Bulk-sequential vs. rsync, same tree, same connection.** Copy the ~9 GB
   ChartData to the card as a single bulk/sequential operation (e.g. one large
   file, or `ditto`/`cp` of a tarball) vs. the normal rsync of the many small
   files. If bulk succeeds where rsync wedges, the write *pattern* is the cause,
   not the driver — and image-dd is confirmed as the right direction.
2. **image-dd once the size matches.** Size a card's FAT32 partition to 25 GiB,
   compose to 25 GiB, run the guard->dd->verify path on this exact
   card/connection. This is the Parallels-equivalent (sequential, no FAT driver).

## CODE GAP worth hardening regardless of root cause

`mount_swap.msdos_mount` step 6 checks ONLY `mount_msdos`'s return code; it never
verifies the resulting mount is actually non-`fskit` (nor that it didn't
hang-then-return-degraded). Add a post-mount assertion that the work_mount's
driver is the kernel msdosfs (no `fskit` tag) and fail/log loudly otherwise, so
every real sync KNOWS it escaped FSKit rather than assuming it.
