#!/bin/bash
# EC2 Setup Script for Poker Tournament Bot
# Run this on a fresh Amazon Linux 2023 or Ubuntu 22.04 EC2 instance
# Usage: bash setup_ec2.sh

set -e

echo "=== Poker Bot EC2 Setup ==="

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
fi

# Install Python and pip
if [ "$OS" = "amzn" ]; then
    echo "Detected Amazon Linux..."
    sudo yum update -y
    sudo yum install -y python3 python3-pip git
elif [ "$OS" = "ubuntu" ]; then
    echo "Detected Ubuntu..."
    sudo apt update && sudo apt upgrade -y
    sudo apt install -y python3 python3-pip python3-venv git
else
    echo "Unsupported OS. Install Python 3 and pip manually."
    exit 1
fi

# Create app directory
APP_DIR=/home/ec2-user/pokerbot
if [ "$OS" = "ubuntu" ]; then
    APP_DIR=/home/ubuntu/pokerbot
fi

mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Copy bot files (assumes you've scp'd them here already)
if [ ! -f poker_bot.py ]; then
    echo ""
    echo "Next steps:"
    echo "  1. Copy your bot files to $APP_DIR/"
    echo "     scp -i your-key.pem poker_bot.py requirements.txt ec2-user@<EC2-IP>:$APP_DIR/"
    echo "  2. Run this script again"
    echo ""
    exit 0
fi

# Set up virtual environment
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Prompt for bot token
if [ ! -f .env ]; then
    echo ""
    read -p "Enter your Telegram BOT_TOKEN: " BOT_TOKEN
    echo "BOT_TOKEN=$BOT_TOKEN" > .env
    chmod 600 .env
    echo "Token saved to .env"
fi

# Install systemd service
sudo cp /home/*/pokerbot/deploy/pokerbot.service /etc/systemd/system/pokerbot.service 2>/dev/null || true

# Determine the user
if [ "$OS" = "ubuntu" ]; then
    SYSUSER="ubuntu"
else
    SYSUSER="ec2-user"
fi

# Create systemd service file
sudo tee /etc/systemd/system/pokerbot.service > /dev/null << SVCEOF
[Unit]
Description=Poker Tournament Telegram Bot
After=network.target

[Service]
Type=simple
User=$SYSUSER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python poker_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

# Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable pokerbot
sudo systemctl start pokerbot

echo ""
echo "=== Setup Complete ==="
echo "Bot is running as a systemd service."
echo ""
echo "Useful commands:"
echo "  sudo systemctl status pokerbot    # Check status"
echo "  sudo systemctl restart pokerbot   # Restart bot"
echo "  sudo systemctl stop pokerbot      # Stop bot"
echo "  sudo journalctl -u pokerbot -f    # View live logs"
