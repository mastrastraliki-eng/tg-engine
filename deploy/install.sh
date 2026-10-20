#!/usr/bin/env bash
# Установка или обновление tg-engine на сервере Ubuntu/Debian как службы systemd.
# Запуск из папки с кодом:  sudo bash deploy/install.sh
# Код копируется в /opt/tg-engine; .env и data/ там не перезаписываются, если уже есть.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/tg-engine

echo "==> Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip fonts-dejavu-core rsync >/dev/null

id tgengine >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin tgengine
mkdir -p "$APP/data"

echo "==> Код в $APP"
if [ "$SRC" != "$APP" ]; then
  rsync -a --delete \
    --exclude .env --exclude data/ --exclude .venv/ --exclude __pycache__/ \
    --exclude .pytest_cache/ --exclude 'backup*' --exclude .git/ \
    "$SRC"/ "$APP"/
  [ -f "$APP/.env" ] || { [ -f "$SRC/.env" ] && cp "$SRC/.env" "$APP/.env"; }
  if [ ! -f "$APP/data/engine.db" ] && [ -f "$SRC/data/engine.db" ]; then
    cp "$SRC/data/engine.db" "$APP/data/engine.db"
  fi
fi
[ -f "$APP/.env" ] || { echo "Нет $APP/.env: скопируй его с компьютера"; exit 1; }
# .env с Windows может прийти с CRLF и BOM
sed -i 's/\r$//; 1s/^\xEF\xBB\xBF//' "$APP/.env"

echo "==> Python-окружение"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

chown -R tgengine:tgengine "$APP"
chmod 600 "$APP/.env"

echo "==> Служба"
install -m 644 "$APP/deploy/tg-engine.service" /etc/systemd/system/tg-engine.service
systemctl daemon-reload
systemctl enable tg-engine >/dev/null
systemctl restart tg-engine
sleep 5
systemctl --no-pager --lines=20 status tg-engine || true
echo
echo "Журнал: journalctl -u tg-engine -f"
