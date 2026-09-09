@echo off
title Speexx Auto-Solver (LM Studio + Playwright)
echo ========================================================
echo   Setting up Speexx Auto-Solver environment...
echo ========================================================

pip install -r requirements.txt
python -m playwright install chromium

echo.
echo ========================================================
echo   Starting Speexx Auto-Solver...
echo ========================================================
python main.py
pause
