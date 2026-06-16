@echo off
title JMComic Server

set JMCOMIC_HOST=127.0.0.1
set JMCOMIC_PORT=8899

set JMCOMIC_DOWNLOAD_DIR=%~dp0data\jmcomic\downloads
set JMCOMIC_TEMP_DIR=%~dp0data\jmcomic\temp

set JMCOMIC_MAX_CONCURRENT=3
set JMCOMIC_DOWNLOAD_TIMEOUT=600
set JMCOMIC_MAX_PAGES=200

set JMCOMIC_CACHE_TTL_HOURS=24
set JMCOMIC_CACHE_CLEANUP_INTERVAL=3600

set JMCOMIC_BLACKLIST_FILE=%~dp0data\jmcomic\blacklist.txt

:: JMCOMIC_PROXY=http://127.0.0.1:7890

echo ========================================
echo   JMComic Server
echo   http://%JMCOMIC_HOST%:%JMCOMIC_PORT%
echo   blacklist: %JMCOMIC_BLACKLIST_FILE%
echo ========================================

cd /d "%~dp0"
python jmcomic_server.py

pause
