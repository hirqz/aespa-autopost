@echo off
title aespa autopost
cd /d "%~dp0"
:loop
python autopost.py
echo Script berhenti. Menjalankan ulang dalam 30 detik... (tutup jendela ini untuk berhenti)
timeout /t 30 /nobreak >nul
goto loop
