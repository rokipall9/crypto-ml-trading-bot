#!/bin/bash
# Daily backup of all bot state + configs.
# Keeps last 30 days, compressed.

BACKUP_DIR="/home/ubuntu/backups"
mkdir -p "$BACKUP_DIR"

DATE=$(date -u +%Y%m%d_%H%M%S)
ARCHIVE="$BACKUP_DIR/backup_$DATE.tar.gz"

# What to back up
tar -czf "$ARCHIVE" \
  --exclude='*.pyc' \
  --exclude='__pycache__' \
  --exclude='*.bak_*' \
  /home/ubuntu/bot/config.py \
  /home/ubuntu/bot/strategy_engine.py \
  /home/ubuntu/bot/srs_main.py \
  /home/ubuntu/bot/run_bot.py \
  /home/ubuntu/bot/bot/*.py \
  /home/ubuntu/bot/.env \
  /home/ubuntu/bot/logs/paper_trades.json \
  /home/ubuntu/daily_signal/*.py \
  /home/ubuntu/daily_signal/state \
  /home/ubuntu/pro_signal/*.py \
  /home/ubuntu/pro_signal/state \
  /home/ubuntu/common \
  2>/dev/null

if [ -f "$ARCHIVE" ]; then
  size=$(du -h "$ARCHIVE" | cut -f1)
  echo "[backup] created $ARCHIVE ($size)"
else
  echo "[backup] FAILED"
  exit 1
fi

# Prune > 30 days old
find "$BACKUP_DIR" -name "backup_*.tar.gz" -mtime +30 -delete
echo "[backup] pruned >30d archives"
ls -la "$BACKUP_DIR" | tail -5
