@echo off
where go >nul 2>nul
if errorlevel 1 (
    echo Go was not found in PATH. Install Go and try again.
    pause
    exit /b 1
)
where gcc >nul 2>nul
if errorlevel 1 (
    echo GCC was not found in PATH. Fyne desktop apps require CGO and a C compiler on Windows.
    echo Install MSYS2/MinGW-w64 and add gcc.exe to PATH, then try again.
    pause
    exit /b 1
)
set CGO_ENABLED=1
go run . %*
