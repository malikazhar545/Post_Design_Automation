@echo off
REM Windows on hone par (Startup se) ya haath se: 5 Chrome profiles + server.
REM Server pichla adhoora run khud dobara shuru karta hai (agar PAUSE par nahi tha).
cd /d "%~dp0"
set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
start "" %CHROME% --profile-directory="Profile 1" --restore-last-session --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding
timeout /t 3 /nobreak >nul
start "" %CHROME% --profile-directory="Profile 31" --restore-last-session --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding
timeout /t 3 /nobreak >nul
start "" %CHROME% --profile-directory="Profile 18" --restore-last-session --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding
timeout /t 3 /nobreak >nul
start "" %CHROME% --profile-directory="Default" --restore-last-session --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding
timeout /t 3 /nobreak >nul
start "" %CHROME% --profile-directory="Profile 2" --restore-last-session --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding
timeout /t 10 /nobreak >nul
start "Post Design Automation - SERVER (band na karna)" /min cmd /k python server.py
