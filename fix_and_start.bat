@echo off
echo [1/4] Killing PM2 and Python processes...
C:\Windows\System32\taskkill.exe /F /IM pm2.exe /T >nul 2>&1
C:\Windows\System32\taskkill.exe /F /IM python.exe /T >nul 2>&1
echo.
echo [2/4] Initializing Telegram connection (Wait 5s)...
C:\Windows\System32\timeout.exe /t 5 /nobreak > nul
echo.
echo [3/4] Starting AutoStock server...
python main.py --restart
echo.
echo [4/4] Done.
pause
