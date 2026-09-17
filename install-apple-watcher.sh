#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_DIR=/opt/apple-watcher
readonly DATA_DIR=/var/lib/apple-watcher
readonly CONFIG_DIR=/etc/apple-watcher
readonly SERVICE_USER=apple-watcher
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if (( EUID != 0 )); then
  echo "请用 sudo 运行此脚本。" >&2
  exit 1
fi
if [[ $(uname -m) != x86_64 ]]; then
  echo "此部署包只支持 x86_64 / AMD64。" >&2
  exit 1
fi
for file in notify.py admin.html upstream/Cargo.toml; do
  [[ -f $SCRIPT_DIR/$file ]] || { echo "缺少 $file，请把部署包完整上传。" >&2; exit 1; }
done

read -rsp "粘贴 Bark 推送地址（输入时不回显）：" BARK_URL
echo
[[ $BARK_URL =~ ^https://[^/]+/.+ ]] || { echo "Bark 地址必须包含 HTTPS 主机和设备 Key。" >&2; exit 1; }
read -rsp "粘贴 Telegram Bot Token（输入时不回显）：" TELEGRAM_TOKEN
echo
[[ $TELEGRAM_TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || { echo "Telegram Bot Token 格式不正确。" >&2; exit 1; }
read -rp "Telegram Chat ID：" TELEGRAM_CHAT_ID
[[ $TELEGRAM_CHAT_ID =~ ^-?[0-9]+$ ]] || { echo "Telegram Chat ID 必须是数字。" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends build-essential ca-certificates clang cmake curl git python3 ufw

if (( $(free -m | awk '/^Swap:/ {print $2}') < 1024 )); then
  swap_file=/swapfile.apple-watcher
  if [[ ! -e $swap_file ]]; then
    fallocate -l 2G "$swap_file"
    chmod 600 "$swap_file"
    mkswap "$swap_file"
  fi
  swapon "$swap_file"
  grep -qF "$swap_file none swap sw 0 0" /etc/fstab || echo "$swap_file none swap sw 0 0" >> /etc/fstab
fi

if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0755 "$APP_DIR" "$CONFIG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR"

if [[ ! -x /root/.cargo/bin/rustup ]]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.98.0
else
  /root/.cargo/bin/rustup toolchain install 1.98.0 --profile minimal
  /root/.cargo/bin/rustup default 1.98.0
fi
/root/.cargo/bin/cargo install --path "$SCRIPT_DIR/upstream/crates/apw-cli" --locked --root "$APP_DIR" --force

install -o root -g root -m 0755 "$SCRIPT_DIR/notify.py" "$APP_DIR/notify.py"
install -o root -g root -m 0644 "$SCRIPT_DIR/admin.html" "$APP_DIR/admin.html"

umask 027
printf '%s\n' "$BARK_URL" > "$CONFIG_DIR/bark-url"
printf '%s\n' "$TELEGRAM_TOKEN" > "$CONFIG_DIR/telegram-token"
printf '%s\n' "$TELEGRAM_CHAT_ID" > "$CONFIG_DIR/telegram-chat-id"
chown root:"$SERVICE_USER" "$CONFIG_DIR"/{bark-url,telegram-token,telegram-chat-id}
chmod 0640 "$CONFIG_DIR"/{bark-url,telegram-token,telegram-chat-id}
if [[ ! -f $DATA_DIR/config.json ]]; then
  runuser -u "$SERVICE_USER" -- /usr/bin/python3 "$APP_DIR/notify.py" --migrate-legacy
fi
unset BARK_URL TELEGRAM_TOKEN TELEGRAM_CHAT_ID

cat > /etc/systemd/system/apple-watcher.service <<'UNIT'
[Unit]
Description=Apple Store inventory watcher with HTTP 541 protection
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=10

[Service]
Type=simple
User=apple-watcher
Group=apple-watcher
ExecStart=/usr/bin/python3 /opt/apple-watcher/notify.py
Restart=always
RestartSec=15
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/apple-watcher

[Install]
WantedBy=multi-user.target
UNIT

ufw allow OpenSSH
ufw --force enable
/usr/bin/python3 "$APP_DIR/notify.py" --self-test
runuser -u "$SERVICE_USER" -- /usr/bin/python3 "$APP_DIR/notify.py" --test
systemctl daemon-reload
systemctl enable apple-watcher.service
systemctl restart apple-watcher.service
systemctl is-active --quiet apple-watcher.service

echo "安装完成：监控与管理页正在运行。"
echo "从电脑建立隧道：ssh -N -L 1420:127.0.0.1:1420 <SSH用户>@<主机地址>"
echo "然后打开：http://127.0.0.1:1420/"
