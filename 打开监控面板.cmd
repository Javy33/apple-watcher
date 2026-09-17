@echo off
if "%APPLE_WATCHER_SSH_HOST%"=="" (
  echo Please set APPLE_WATCHER_SSH_HOST to user@host first.
  exit /b 1
)
start "Apple Watcher SSH" /min ssh -N -L 1420:127.0.0.1:1420 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 %APPLE_WATCHER_SSH_HOST%
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:1420/
