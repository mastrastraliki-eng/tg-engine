"""Вносит ключи из keys-new.env в .env сервера: существующие строки заменяет, новые дописывает.
Значения не печатает. Запуск: python3 merge_env.py keys-new.env /opt/tg-engine/.env"""
import sys
from pathlib import Path

new_path, env_path = Path(sys.argv[1]), Path(sys.argv[2])
if not new_path.exists():
    sys.exit(0)
new = {}
for line in new_path.read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        if v.strip():
            new[k.strip()] = v.strip()
lines = env_path.read_text(encoding="utf-8-sig").splitlines() if env_path.exists() else []
out, done = [], set()
for line in lines:
    k = line.split("=", 1)[0].strip()
    if k in new and not line.lstrip().startswith("#"):
        out.append(f"{k}={new[k]}")
        done.add(k)
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in new.items() if k not in done]
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
print("keys updated:", ", ".join(sorted(new)) or "none")
