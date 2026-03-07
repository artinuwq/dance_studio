# Deploy Scripts

Scripts:
- `install-service.sh` installs/updates one `systemd` unit with path/user substitution.
- `deploy.sh` does fetch/checkout/pull, restarts services, runs healthcheck.
- `rollback.sh` switches `current` to `previous`, reinstalls unit files, restarts services, runs healthcheck.
- `healthcheck.sh` checks a URL with timeout and retries.

## Default Service Mode

By default, deploy/rollback work with split services:
- `web`
- `bot`

Legacy combined unit `dance-studio.service` is kept only for backward compatibility.

You can override with:
- `--service <name>` (repeatable)
- `--services "name1 name2"`

## Expected Server Layout

Defaults:
- `DEPLOY_ROOT=/opt/dance_studio`
- `CURRENT_LINK=/opt/dance_studio/current`
- `PREVIOUS_LINK=/opt/dance_studio/previous`
- `ENV_FILE=/opt/dance_studio/.env`

## Examples

Install units:

```bash
sudo ./deploy/install-service.sh web \
  --app-dir /opt/dance_studio/current \
  --app-user dance \
  --app-group dance \
  --env-file /opt/dance_studio/.env

sudo ./deploy/install-service.sh bot \
  --app-dir /opt/dance_studio/current \
  --app-user dance \
  --app-group dance \
  --env-file /opt/dance_studio/.env
```

Deploy `main` (web + bot):

```bash
sudo ./deploy/deploy.sh \
  --branch main \
  --services "web bot"
```

Deploy only web:

```bash
sudo ./deploy/deploy.sh \
  --branch main \
  --service web
```

Manual rollback (web + bot):

```bash
sudo ./deploy/rollback.sh \
  --services "web bot" \
  --deploy-root /opt/dance_studio
```

Run healthcheck manually:

```bash
HEALTHCHECK_URL="http://127.0.0.1:3000/health" ./deploy/healthcheck.sh
```

## Notes

- `deploy.sh`, `rollback.sh`, `install-service.sh` require `root`.
- Make scripts executable on server:

```bash
chmod +x deploy/*.sh
```
