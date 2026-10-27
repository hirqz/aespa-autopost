@echo off
cd /d "%~dp0"
echo === Install library Python ===
python -m pip install -r requirements.txt
echo.
echo === Install browser cadangan (dipakai kalau Google Chrome tidak ditemukan) ===
python -m playwright install chromium
echo.
if not exist secrets.json copy secrets.example.json secrets.json >nul
echo Selesai. Buka secrets.json dengan Notepad dan isi BOT_TOKEN.
pause
