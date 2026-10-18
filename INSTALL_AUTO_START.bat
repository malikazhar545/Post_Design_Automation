@echo off
REM Windows login par AUTO_START khud chale (bijli / restart ke baad kaam khud resume)
set S=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Post Automation.bat
echo @call "%~dp0AUTO_START.bat" > "%S%"
echo Auto-start lag gaya: %S%
pause
