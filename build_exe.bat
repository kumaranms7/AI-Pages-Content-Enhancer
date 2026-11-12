@echo off
REM Rebuilds the desktop .exe after any code change. Run this from the
REM project folder (double-click works too). Output goes to dist\.
cd /d "%~dp0"
venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed ^
  --name "AI Page Content Enhancer" ^
  --icon "assets\icon.ico" ^
  --add-data "assets;assets" ^
  --collect-all customtkinter ^
  app.py
echo.
echo Done. The exe is in the dist folder.
pause
