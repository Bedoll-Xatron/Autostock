import subprocess
def find_all_pids():
    try:
        r = subprocess.run(
            'C:\\Windows\\System32\\wbem\\wmic.exe process where "name=\'python.exe\'" get ProcessId,CommandLine /format:csv',
            capture_output=True, shell=True,
        )
        output = r.stdout.decode("cp949", errors="replace")
        print("--- Active Python Processes ---")
        print(output)
    except Exception as e:
        print(e)

find_all_pids()
