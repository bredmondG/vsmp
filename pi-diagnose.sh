#!/bin/sh
# Read-only diagnosis of why writes on the Pi do not survive a reboot.
# Changes nothing. Run with:  sh pi-diagnose.sh

echo "=== 1. how is / mounted? ==="
findmnt -no SOURCE,FSTYPE,OPTIONS /
echo
echo "if FSTYPE is 'overlay'      -> the overlay filesystem is active"
echo "if OPTIONS contains 'ro'    -> the root is read-only"

echo
echo "=== 2. where is the boot partition, and is it writable? ==="
for d in /boot/firmware /boot; do
  if findmnt -no TARGET "$d" >/dev/null 2>&1; then
    printf '%-16s ' "$d"
    findmnt -no SOURCE,FSTYPE,OPTIONS "$d"
  fi
done
echo
echo "a boot mount with 'ro' is the blocker: raspi-config cannot save the"
echo "change that disables overlay, so the setting never takes effect"

echo
echo "=== 3. is overlayroot still requested in cmdline.txt? ==="
for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
  [ -f "$f" ] && { printf '%s:\n  ' "$f"; cat "$f"; }
done
echo
for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
  [ -f "$f" ] && grep -o 'overlayroot=[^ ]*' "$f" && echo "  ^ overlay IS still enabled in $f"
done

echo
echo "=== 4. overlayroot config ==="
[ -f /etc/overlayroot.conf ] && grep -vE '^\s*#|^\s*$' /etc/overlayroot.conf || echo "(no /etc/overlayroot.conf)"

echo
echo "=== 5. fstab: look for 'ro' on / or the boot partition ==="
grep -vE '^\s*#|^\s*$' /etc/fstab

echo
echo "=== 6. all read-only mounts ==="
findmnt -rno TARGET,FSTYPE,OPTIONS | grep -E '(^| )ro(,|$)|,ro,|,ro$' || echo "(none)"

echo
echo "=== 7. filesystem health ==="
dmesg | grep -iE "ext4-fs|fat-fs|recovery required|not properly unmounted|remounting|i/o error|mmc" | tail -20

echo
echo "=== 8. power supply and temperature history ==="
if command -v vcgencmd >/dev/null 2>&1; then
  raw=$(vcgencmd get_throttled 2>/dev/null)
  echo "$raw"
  n=$(( ${raw#throttled=} ))

  # Bits 0-3 are live state; bits 16-19 are sticky "has happened since boot".
  # Only the under-voltage bits point at the power supply. The temperature bits
  # are a separate concern and are usually benign.
  echo "  decoded:"
  if [ "$n" -eq 0 ]; then
    echo "    nothing to report: no under-voltage, no throttling since boot"
  fi
  [ $(( n & 0x1 ))     -ne 0 ] && echo "    NOW  under-voltage          <- POWER SUPPLY PROBLEM"
  [ $(( n & 0x2 ))     -ne 0 ] && echo "    NOW  arm frequency capped"
  [ $(( n & 0x4 ))     -ne 0 ] && echo "    NOW  throttled"
  [ $(( n & 0x8 ))     -ne 0 ] && echo "    NOW  soft temperature limit active"
  [ $(( n & 0x10000 )) -ne 0 ] && echo "    PAST under-voltage occurred  <- POWER SUPPLY PROBLEM"
  [ $(( n & 0x20000 )) -ne 0 ] && echo "    PAST arm frequency capped"
  [ $(( n & 0x40000 )) -ne 0 ] && echo "    PAST throttled (hard limit, around 80-85C)"
  [ $(( n & 0x80000 )) -ne 0 ] && echo "    PAST soft temperature limit reached (around 60C) - usually benign"

  echo "  interpretation:"
  if [ $(( n & 0x10001 )) -ne 0 ]; then
    echo "    Under-voltage is present or has happened. Replace the power supply"
    echo "    or the USB cable. No code change can fix this."
  else
    echo "    No under-voltage at any point since boot, so the power supply is not"
    echo "    the problem. Temperature bits alone do not cause stalls; they only"
    echo "    make ffmpeg extraction slower. Compare the extract_s= values in"
    echo "    log.txt against WatchdogSec in the unit if throttling is present."
  fi
else
  echo "(vcgencmd unavailable)"
fi

echo
echo "--- current temperature and clock ---"
vcgencmd measure_temp 2>/dev/null || echo "(vcgencmd unavailable)"
vcgencmd get_config temp_soft_limit 2>/dev/null
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq ]; then
  printf 'arm clock now: '
  awk '{ printf "%d MHz\n", $1/1000 }' /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
fi
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq ]; then
  printf 'arm clock max: '
  awk '{ printf "%d MHz\n", $1/1000 }' /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq
fi

echo
dmesg | grep -iE "under-voltage|throttl" | tail -5 || echo "(no undervoltage or throttling messages in dmesg)"

echo
echo "=== 9. SD card identity and lifetime ==="
for f in /sys/block/mmcblk0/device/name /sys/block/mmcblk0/device/manfid /sys/block/mmcblk0/device/life_time; do
  [ -f "$f" ] && printf '%-44s %s\n' "$f" "$(cat "$f" 2>/dev/null)"
done

echo
echo "=== 10. did the service ever get enabled? ==="
systemctl is-enabled vsmp 2>&1
ls -la /etc/systemd/system/vsmp.service 2>&1
ls -la /etc/default/vsmp 2>&1

echo
echo "=== 11. can the player actually persist its position? ==="
# state.json is where the player records which frame it is on, rewritten after
# every frame. If it cannot be written, or is written to a filesystem that is
# discarded on reboot, the movie silently restarts from the beginning every time
# the Pi is power-cycled -- which looks like the player losing its place rather
# than like a filesystem problem. That is the specific failure this whole script
# is here to find, so this is the check that connects it to a visible symptom.
for d in /home/pi/vsmp .; do
  [ -d "$d" ] || continue
  echo "--- $d ---"
  ls -la "$d/state.json" 2>&1
  if [ -w "$d" ]; then
    if touch "$d/.vsmp-write-probe" 2>/dev/null; then
      echo "  directory is writable now (probe created and removed)"
      rm -f "$d/.vsmp-write-probe"
    else
      echo "  DIRECTORY NOT WRITABLE  <- the player cannot save its position"
    fi
  else
    echo "  DIRECTORY NOT WRITABLE  <- the player cannot save its position"
  fi
  # Which filesystem is it on? An overlay upper layer in tmpfs is lost on reboot.
  findmnt -no SOURCE,FSTYPE,OPTIONS --target "$d" 2>/dev/null
done
echo
echo "if state.json exists but its mtime never advances -> the player is not"
echo "  progressing; check 'python3 vsmp.py status' and log.txt"
echo "if the filesystem above is overlay/tmpfs -> writes are discarded on reboot"
echo "  and the movie restarts from frame 0 every power cycle"

echo
echo "=== 12. is the old sliced-sections layout still taking up space? ==="
# The current player seeks into the single movie file and never uses these.
# They are often several GB.
find . -maxdepth 2 -name '*_section*.mp4' -print 2>/dev/null | head -5
find . -maxdepth 2 -name '*_section*.mp4' 2>/dev/null | wc -l | \
  awk '{ if ($1 > 0) print "  " $1 " section file(s) found -- no longer used, see DEPLOY.md step 4" }'
find . -maxdepth 2 -name 'out_img*.jpg' 2>/dev/null | wc -l | \
  awk '{ if ($1 > 0) print "  " $1 " old frame file(s) found -- cleared automatically on next start" }'
[ -f progress.pkl ] && echo "  progress.pkl present -- superseded by state.json, not migrated (see DEPLOY.md step 4)"

echo
echo "=== done ==="
