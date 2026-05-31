@echo off
echo Southern GA + Northeast FL Foreclosure Scraper
echo ==============================================

REM Check if venv exists, create if not
if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

REM Install/upgrade deps silently
pip install -r requirements.txt -q
python -m playwright install chromium

REM Run scraper first
echo.
echo Starting admin portal at http://localhost:5000
echo Press Ctrl+C to stop.
python app.py
