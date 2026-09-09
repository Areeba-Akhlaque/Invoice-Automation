@echo off
REM Local Windows Task Scheduler entry (alternative to GitHub Actions).
REM Runs daily; --auto exits unless today is a configured run day (7th / 22nd).
REM Generates a draft tab (Kimai hours + fixed 86.5 + AI descriptions); you then
REM fill James/Bradd/Keeko hours, pass-through $ amounts, and review.

cd /d "d:\Invoice Automation"
"C:\Users\HP\AppData\Local\Python\pythoncore-3.14-64\python.exe" run.py --auto --write --yes >> "d:\Invoice Automation\scheduled.log" 2>&1
