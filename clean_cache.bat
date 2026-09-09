@echo off
chcp 65001 >nul
echo Closing open Chrome and Python processes...
taskkill /F /IM chrome.exe /T >nul 2>&1

echo Cleaning Cache, Browser Profile and Speexx Memory...
if exist browser_profile rmdir /s /q browser_profile
if exist browser_profile_session rmdir /s /q browser_profile_session
if exist speexx_memory.json del /f /q speexx_memory.json
if exist logs rmdir /s /q logs
if exist diagnose_output.txt del /f /q diagnose_output.txt

echo.
echo [DONE] Cleaned cache and session files successfully!
pause
