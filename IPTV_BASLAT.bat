@echo off
chcp 65001 >nul
title IPTV Player sunucusu
cd /d "%~dp0"
python -u server.py
pause
