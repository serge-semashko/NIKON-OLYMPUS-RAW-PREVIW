@echo off
if not exist venv\Scripts\python.exe (
    echo Virtual environment not found. Run init_venv.bat first.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
echo Starting ORF Explorer...
python main.py %*
