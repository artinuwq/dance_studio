# Deploy Scripts

Скрипты:
- `install-service.sh` — установка/обновление одного `systemd` unit с подстановкой путей и пользователя.
- `deploy.sh` — рабочий деплой: `fetch/checkout/pull`, зависимости, build, миграции, рестарт, healthcheck, rollback при ошибке.
- `rollback.sh` — переключение `current` на `previous`, рестарт сервиса, healthcheck.
- `healthcheck.sh` — проверка URL с таймаутом и ретраями.

## Ожидаемая структура на сервере

По умолчанию:
- `DEPLOY_ROOT=/opt/dance_studio`
- `REPO_DIR=/opt/dance_studio/repo`
- `RELEASES_DIR=/opt/dance_studio/releases`
- `CURRENT_LINK=/opt/dance_studio/current`
- `PREVIOUS_LINK=/opt/dance_studio/previous`
- `ENV_FILE=/opt/dance_studio/.env`

## Примеры

Установка unit:

```bash
sudo ./deploy/install-service.sh run_all \
  --app-dir /opt/dance_studio/current \
  --app-user dance \
  --app-group dance \
  --env-file /opt/dance_studio/.env
```

Деплой ветки `main`:

```bash
sudo ./deploy/deploy.sh \
  --branch main \
  --service run_all \
  --deploy-root /opt/dance_studio
```

Ручной rollback:

```bash
sudo ./deploy/rollback.sh \
  --service run_all \
  --deploy-root /opt/dance_studio
```

Проверка health вручную:

```bash
HEALTHCHECK_URL="http://127.0.0.1:3000/health" ./deploy/healthcheck.sh
```

## Важно

- Скрипты `deploy.sh`, `rollback.sh`, `install-service.sh` требуют `root` (пишут в `/etc/systemd/system`, делают `systemctl`).
- Сделайте их исполняемыми на сервере:

```bash
chmod +x deploy/*.sh
```
