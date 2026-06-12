import os
import time

def check_recent_modifications(directory, duration_seconds=7200):
    now = time.time()
    modified_files = []
    
    # Exclude common ignores
    exclude_dirs = {'.git', '.claude', '__pycache__', 'logs', '.venv', 'venv', 'env', 'node_modules', '.pytest_cache'}
    
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for file in files:
            full_path = os.path.join(root, file)
            try:
                mtime = os.path.getmtime(full_path)
                if now - mtime < duration_seconds:
                    modified_files.append((full_path, mtime))
            except Exception:
                pass
    return modified_files

print("--- Checking modified files in chutzrit/autostock (last 2 hours) ---")
for f, m in sorted(check_recent_modifications('d:\\INFORUN\\chutzrit\\autostock'), key=lambda x: x[1]):
    print(f" - {f} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(m))})")

print("\n--- Checking modified files in HoDoo/Part7 (last 2 hours) ---")
for f, m in sorted(check_recent_modifications('d:\\INFORUN\\HoDoo\\Part7'), key=lambda x: x[1]):
    print(f" - {f} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(m))})")
