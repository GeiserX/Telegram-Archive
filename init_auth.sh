#!/bin/bash

echo "=========================================="
echo "Telegram Backup - Authentication Setup"
echo "=========================================="
echo

if [ ! -f .env ]; then
    echo "[ERROR] .env file not found!"
    echo "Please copy .env.example to .env and fill in your credentials."
    exit 1
fi

mkdir -p data/backups

echo "Starting interactive authentication container..."
echo "You will be asked for your Telegram verification code."
echo

docker-compose run --rm telegram-backup python -m src.setup_auth

if [ $? -eq 0 ]; then
    echo
    echo "[SUCCESS] Authentication completed!"
    echo "You can now run 'docker-compose up -d' to start the backup service."
else
    echo
    echo "[ERROR] Authentication failed."
fi
