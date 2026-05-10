import psutil

default_procs = []
for p in psutil.process_iter(['pid', 'name', 'cmdline']):
    try:
        cl = ' '.join(p.info['cmdline'] or [])
        if p.info['name'].lower() == 'chrome.exe':
            is_default = r'P:\\\\\.data\yt-is\browser\notebooklm --' in cl
            is_lane = 'notebooklm-pro' in cl or 'notebooklm-free' in cl
            if is_default and not is_lane:
                default_procs.append(p.info['pid'])
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

print(f'Default profile processes remaining: {len(default_procs)}: {default_procs}')