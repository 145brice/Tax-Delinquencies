@echo off
echo Nashville Tax Scraper + Admin Portal
echo =====================================

REM Check if venv exists, create if not
if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

REM Install/upgrade deps silently
pip install -r requirements.txt -q

REM Run scraper first
echo.
echo Running scraper for all counties...
python scraper_runner.py

echo.
echo Starting admin portal at http://localhost:5000
echo Press Ctrl+C to stop.
python app.py
