@echo off
echo Creating virtual environment...
python -m venv venv
if errorlevel 1 (
    echo Failed to create venv. Make sure Python 3.10+ is installed.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Virtual environment ready!
echo Run 'run_venv.bat' to start the app.
pause
