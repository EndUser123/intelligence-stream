"""Clean shutdown of all lane Chrome processes with sub-process drain."""
import psutil, time, subprocess, os, sys

roots = [
    r'P:\\\\.data\yt-is\browser\notebooklm-pro',
    r'P:\\\\.data\yt-is\browser\notebooklm-free',
]

def get_lane_chrome_pids():
    pids = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cl = ' '.join(p.info['cmdline'] or [])
            if p.info['name'].lower() == 'chrome.exe' and any(r in cl for r in roots):
                pids.append(p.info['pid'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids

print("Step 1: Terminate all lane Chrome processes...")
all_killed = []
for p in psutil.process_iter(['pid', 'name', 'cmdline']):
    try:
        cl = ' '.join(p.info['cmdline'] or [])
        if p.info['name'].lower() == 'chrome.exe' and any(r in cl for r in roots):
            p.kill()
            all_killed.append(p.info['pid'])
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
print(f"  Terminated {len(all_killed)} processes: {all_killed}")

print("\nStep 2: Wait for sub-process drain (up to 90s)...")
start = time.monotonic()
deadline = start + 90.0
last_report = -1
while time.monotonic() < deadline:
    time.sleep(3)
    remaining = get_lane_chrome_pids()
    elapsed = time.monotonic() - start
    if not remaining:
        print(f"  All lane Chrome processes exited after {elapsed:.1f}s")
        break
    report_interval = int(elapsed / 10)
    if report_interval != last_report:
        print(f"  [{elapsed:.0f}s] Still running: {sorted(remaining)}")
        last_report = report_interval
else:
    remaining = get_lane_chrome_pids()
    print(f"\nWARNING: {len(remaining)} lane processes still alive after 90s")

print("\nStep 3: Verify clean state for browser health gate...")
default_procs = []
for p in psutil.process_iter(['pid', 'name', 'cmdline']):
    try:
        cl = ' '.join(p.info['cmdline'] or [])
        if p.info['name'].lower() == 'chrome.exe':
            is_default = r'P:\\\\.data\yt-is\browser\notebooklm --' in cl
            is_lane = 'notebooklm-pro' in cl or 'notebooklm-free' in cl
            if is_default and not is_lane:
                default_procs.append(p.info['pid'])
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

lane_pids = get_lane_chrome_pids()
print(f"  Default profile Chrome: {len(default_procs)} (should be 0)")
print(f"  Lane profile Chrome: {len(lane_pids)} (should be 0)")

if default_procs or lane_pids:
    print("\nFAILED: Chrome processes still running")
    sys.exit(1)
else:
    print("\nPASSED: Clean browser state confirmed")
    sys.exit(0)