@echo off
echo ==========================================
echo Telegram Backup - Authentication Setup
echo ==========================================
echo.

if not exist .env (
    echo [ERROR] .env file not found!
    echo Please copy .env.example to .env and fill in your credentials.
    pause
    exit /b 1
)

if not exist data\backups (
    mkdir data\backups
)

echo Starting interactive authentication container...
echo You will be asked for your Telegram verification code.
echo.

docker-compose run --rm telegram-backup python -m src.setup_auth

echo.
if %ERRORLEVEL% EQU 0 (
    echo [SUCCESS] Authentication completed!
    echo You can now run 'docker-compose up -d' to start the backup service.
) else (
    echo [ERROR] Authentication failed.
)
pause
