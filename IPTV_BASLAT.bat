@echo off
chcp 65001 >nul
title IPTV Player sunucusu
cd /d "%~dp0"
echo.
echo  IPTV Player calisiyor. Bu pencere acik kaldigi surece iPad'den acabilirsiniz.
echo.
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
  for /f "tokens=* delims= " %%b in ("%%a") do echo    iPad adresi:  http://%%b:8795/
)
echo.
echo  Kapatmak icin bu pencereyi kapatin.
echo.
python -m http.server 8795 --bind 0.0.0.0
pause
