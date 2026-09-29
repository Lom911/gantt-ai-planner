# Runbook: эксплуатация Gantt AI Planner в продакшене

Этот документ описывает операции с продакшен-стендом на VPS: первичную
настройку сервера, регулярный деплой и откат, ротацию секретов, проверку
восстановления из бэкапа и чек-лист на случай инцидента.

Сервер общий с другими сервисами — ничего из описанного здесь не должно
затрагивать их порты, сети или конфигурацию. Скрипты в `deploy/` тоже
этого не делают: `deploy/bootstrap.sh` работает только с пользователем
`deploy`, каталогами `/opt/gantt-planner`, `/opt/caddy`,
`/etc/gantt-planner`, сетями Docker `edge` и `planner-proxy` и
cron-задачей бэкапа.

## Обзор стенда

- `/opt/gantt-planner` — стек приложения (compose-проект `gantt-planner`):
  `compose.prod.yml`, `.env` (`IMAGE_TAG`, `LLM_PROVIDER`, `LLM_MODEL`),
  `secrets/` (0700, файлы 0444: пароли БД, ключи LLM, `ops_token` —
  токен эндпоинта `/api/ops/status`), `initdb/10-roles.sh`.
- `/var/lib/gantt-planner` смонтирован в `app` только для чтения (оттуда
  приложение читает `backup-status` для `/api/ops/status`; каталог `pg/`
  внутри — 0700 uid 70, приложению (uid 10001) недоступен).
- `/var/lib/gantt-planner/pg` — данные Postgres (bind-mount).
- `/var/backups/gantt-planner` — дампы `pg_dump`: ночные `<дата>.dump` и
  снапшоты перед каждым деплоем `pre-deploy-<UTC>.dump`, те и другие
  хранятся 7 дней.
- `/var/lib/gantt-planner/backup-status` — итог последнего ночного бэкапа
  и его офсайт-копии (раздел 4).
- `/etc/gantt-planner` (0700, root) — настройки офсайт-копий:
  `offsite.conf`, публичный сертификат `backup-recipient.pem`, deploy-ключ
  `backup-deploy-key`, `github_known_hosts` (раздел 4, «Офсайт-копии»).
- `/opt/caddy` — общий reverse proxy (Caddy 2), обслуживает
  `gantt-ai-planner.duckdns.org` вместе с другими сайтами на этом хосте.
- Сети Docker:
  - `edge` (внешняя) — Caddy и upstream'ы других сайтов хоста; приложения
    в ней нет;
  - `planner-proxy` (внешняя, `--internal`, без выхода наружу) — только
    Caddy и контейнер `app` (алиас `planner-app`); кроме Caddy, до
    приложения не достучится ни один контейнер хоста;
  - `gantt-planner_backend` (internal) — `app`/`migrate` ↔ `db`;
  - `gantt-planner_egress` — отдельная сеть проекта, только у `app`:
    исходящий HTTPS к API LLM (OpenRouter/Anthropic).
- Образ приложения: `ghcr.io/lom911/gantt-ai-planner`, теги
  `sha-<короткий-sha>` и `latest`. Теги в GHCR изменяемые, поэтому сервер
  принимает **только** ссылку с неизменяемым digest
  (`sha-<short>@sha256:<digest>`) — и от CD, и при ручном деплое/откате;
  как узнать digest — раздел 2, «Где взять digest».

## 1. Первичная настройка сервера

Выполняется один раз, вручную, от root.

1. Скопировать репозиторий (или только каталог `deploy/`) на сервер.
2. Запустить `sudo bash deploy/bootstrap.sh`. Скрипт идемпотентен, каждый
   шаг печатает, что делает:
   - создаёт системного пользователя `deploy` (без группы `docker`);
   - берёт ту же блокировку, что `planner-deploy`
     (`/run/lock/planner-deploy.lock`), чтобы не менять файлы под идущим
     деплоем;
   - устанавливает `/usr/local/bin/planner-deploy`,
     `/usr/local/bin/planner-deploy-wrapper`,
     `/usr/local/bin/gantt-planner-backup.sh`,
     `/usr/local/bin/gantt-planner-offsite-backup.sh`;
   - устанавливает правило sudoers `/etc/sudoers.d/gantt-planner-deploy`
     (провалидировано `visudo -cf` перед установкой);
   - создаёт каталоги `/opt/gantt-planner`, `/opt/gantt-planner/secrets`
     (0700, root), `/var/lib/gantt-planner/pg` (владелец uid:gid 70:70,
     как у postgres в образе `postgres:17-alpine`), `/var/backups/gantt-planner`,
     `/opt/caddy`;
   - копирует `compose.prod.yml`, `initdb/10-roles.sh`, конфиг Caddy
     (существующие `/opt/caddy/compose.yml` и `Caddyfile` не
     перезаписывает — см. «Существующий стек Caddy» ниже) и **прерывается
     с кодом 1**, если стек Caddy не подключён к сети `planner-proxy` или в
     `Caddyfile` нет `reverse_proxy planner-app:8000` (печатает, что именно
     добавить); если Caddy запущен без hardening'а — только предупреждает;
   - создаёт `/opt/gantt-planner/.env` (если его нет) с `LLM_PROVIDER` и
     `LLM_MODEL`, **без `IMAGE_TAG`**: тег первого релиза задаётся явно в
     п. 4, до этого `docker compose` для этого стека падает с
     `required variable IMAGE_TAG is missing a value` — молчаливого
     отката на `latest` больше нет;
   - генерирует `db_app_password`, `db_owner_password`,
     `pg_superuser_password` через `openssl rand -base64 32` — **только
     если файлов ещё нет**, существующие пароли не трогает. Пароль пишется
     во временный файл в том же каталоге и переименовывается на место
     только если он непустой: упавший `openssl` не оставит пустой секрет,
     который следующий запуск принял бы за готовый;
   - генерирует `secrets/ops_token` (`openssl rand -hex 32`, тем же
     способом — только если его нет) и печатает `ACTION NEEDED`: значение
     нужно положить в секрет GitHub Actions `OPS_TOKEN` (см. п. 3); пустой
     каталог на месте секрета (его создаёт Docker, если `up` запускали без
     файла) заменяется файлом;
   - создаёт пустые файлы-заглушки `secrets/anthropic_api_key` и
     `secrets/openrouter_api_key`, если их нет;
   - создаёт сеть Docker `edge` и внутреннюю сеть `planner-proxy`
     (`docker network create --internal planner-proxy`), если их нет;
     существующую `planner-proxy` без `--internal` не принимает;
   - поднимает стек Caddy (`docker compose up -d` в `/opt/caddy`);
   - устанавливает cron `/etc/cron.d/gantt-planner-backup` (03:15 каждый
     день);
   - создаёт `/etc/gantt-planner` (0700) и кладёт туда
     `github_known_hosts` (закреплённый ключ хоста github.com) и сообщает,
     настроены ли офсайт-копии (раздел 4, «Офсайт-копии»; пока нет —
     бэкапы остаются только локальными).
3. После bootstrap вручную:
   - убедиться, что пакет `ghcr.io/lom911/gantt-ai-planner` публичный,
     иначе `docker compose pull` на сервере (без залогина в GHCR) не сможет
     скачать образ. Пакет, опубликованный из Actions публичного
     репозитория, наследует его видимость (так и вышло на этом проде); если
     он всё же приватный — GitHub → профиль/организация → Packages →
     `gantt-ai-planner` → Package settings → Change visibility → Public;
   - вписать реальный ключ LLM — по умолчанию (`LLM_PROVIDER=openrouter`,
     который bootstrap уже прописал в `/opt/gantt-planner/.env`) это ключ
     OpenRouter (`sk-or-v1-...`) в
     `/opt/gantt-planner/secrets/openrouter_api_key`; если вместо этого
     нужен настоящий ключ Anthropic — `secrets/anthropic_api_key` и
     `LLM_PROVIDER=anthropic` в `.env` (без переноса строки в конце и не
     через аргумент команды — см. «Ключ OpenRouter» / «Ключ Anthropic» и
     «Переключение провайдера LLM» в разделе 3).
     Пока владелец не заполнил нужный файл (bootstrap создаёт оба
     пустыми), приложение работает в демо-режиме без LLM — `make_llm()`
     видит пустой ключ, логирует это (без содержимого ключа) и отдаёт
     `FakeLLM` вместо `AnthropicLLM`, само приложение при этом не падает;
     текущий режим виден в ответе `GET /api/meta` (`llm_mode`) и
     значком в UI;
   - настроить GitHub Environment `production` (Settings → Environments →
     `production`) — **обязательно до первого мёржа в `main`**, это
     внешняя настройка, в репозитории её не видно:
     - **Required reviewers** (по желанию) — владелец репозитория: каждый
       деплой ждёт ручного подтверждения; на текущем проде не включено,
       деплой идёт автоматически после зелёного CI;
     - **Deployment branches and tags** → Selected branches → `main`.
     `deploy.yml` сам проверяет, что запуск пришёл из `push` в `main` этого
     репозитория (а не из PR форка с веткой `main`) или что
     `workflow_dispatch` запущен на `main`; настройки окружения — второй
     рубеж на случай ошибки в этом условии. Секреты `DEPLOY_SSH_KEY` и
     `DEPLOY_KNOWN_HOSTS` хранить только в этом окружении, не в секретах
     репозитория;
   - скопировать токен ops-эндпоинта в секрет репозитория `OPS_TOKEN`
     (Settings → Secrets and variables → Actions → New repository secret;
     workflow `Uptime` не привязан к окружению `production`):
     `sudo cat /opt/gantt-planner/secrets/ops_token` и вставить значение
     в форму (не передавать его аргументом команды). Пока секрета нет,
     Uptime пропускает проверку метрик с notice;
   - добавить публичный deploy-ключ CI в
     `/home/deploy/.ssh/authorized_keys` строкой вида:
     ```
     restrict,command="/usr/local/bin/planner-deploy-wrapper" ssh-ed25519 AAAA... gha-gantt-planner
     ```
   - попросить владельца VPS создать A-запись DuckDNS
     `gantt-ai-planner.duckdns.org` → IP сервера (токен DuckDNS на
     сервере не нужен, поддомен создаёт владелец);
   - убедиться, что порты 80/443 доступны из интернета (это единственная
     проверка сетевого доступа, которая нужна при первом деплое).
4. Первый деплой — вручную, без CD, с явной ссылкой **с digest**
   (`planner-deploy` без текущего `IMAGE_TAG` в `.env` отказывается
   работать — ему не на что откатываться; значение без digest он тоже не
   примет как цель отката):
   ```bash
   cd /opt/gantt-planner
   # digest образа sha-<commit> (см. раздел 2, «Где взять digest»):
   docker buildx imagetools inspect ghcr.io/lom911/gantt-ai-planner:sha-<commit> --format '{{.Manifest.Digest}}'
   # Дописать (>>, а не >: в .env уже лежат LLM_PROVIDER/LLM_MODEL).
   echo "IMAGE_TAG=sha-<commit>@sha256:<digest>" >> .env
   docker compose -f compose.prod.yml pull
   docker compose -f compose.prod.yml up -d
   docker compose -f compose.prod.yml logs -f migrate app
   ```
   Проверить `https://gantt-ai-planner.duckdns.org/healthz` — должен
   отвечать `200`.

### Существующий стек Caddy (общий с другими сайтами)

`bootstrap.sh` никогда не перезаписывает `/opt/caddy/compose.yml` и
`Caddyfile` (там могут быть другие сайты). Зато проверяет их по
нормализованному `docker compose config` и **останавливается (fail
closed)**, если ни один сервис стека не подключён к сети
`planner-proxy` (по ключу или по `name:`) или в `Caddyfile` нет строки
`reverse_proxy planner-app:8000` — иначе при пересоздании `app` сайт
ответит 502. Сообщение `ABORT: …` содержит точное исправление; после
него bootstrap запускается заново. Подключить Caddy к сети нужно **до**
пересоздания контейнера `app`:

```yaml
# /opt/caddy/compose.yml — добавить (остальное не трогать)
networks:
  planner-proxy:
    external: true

services:
  caddy:
    networks:
      - edge
      - planner-proxy
```

```bash
docker network inspect planner-proxy >/dev/null 2>&1 || docker network create --internal planner-proxy
cd /opt/caddy && docker compose config -q && docker compose up -d   # пересоздаст caddy: пауза в несколько секунд для всех сайтов хоста
docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$(docker compose ps -q caddy)"   # edge planner-proxy
```

`Caddyfile` менять не нужно, если в нём уже есть блок
`gantt-ai-planner.duckdns.org` из `deploy/caddy/Caddyfile`:
`reverse_proxy planner-app:8000` находит приложение по алиасу в
`planner-proxy`.

**Hardening Caddy.** `deploy/caddy/compose.yml` запускает Caddy (единственный
контейнер этой схемы, смотрящий в интернет) с `read_only: true` (корневая
ФС только для чтения; писать можно в тома `caddy_data` — сертификаты и
состояние ACME — и `caddy_config`, плюс `tmpfs` в `/tmp`),
`cap_drop: [ALL]` + `cap_add: [NET_BIND_SERVICE]` (только привязка к
80/443), `security_opt: [no-new-privileges:true]` и `pids_limit: 128`.
Проверено локально на том же образе (`caddy:2@sha256:0c99…`): Caddy
стартует, в процессе остаётся только `CAP_NET_BIND_SERVICE`, отдаёт HTTP,
выпускает сертификат `tls internal` и пишет CA и сертификат в том данных
(тот же путь хранения, что у ACME), сертификат переживает пересоздание
контейнера, запись в корневую ФС отклоняется, боевой `Caddyfile` проходит
`caddy validate`. Саму выдачу сертификата Let's Encrypt (HTTP-01) офлайн
не проверить — после включения на проде смотреть
`docker compose logs caddy` на ошибки `obtaining certificate` при
следующем продлении. На существующем общем стеке эти ключи вносятся в
`/opt/caddy/compose.yml` руками (bootstrap файл не трогает и только
напоминает `NOTE: Caddy runs without the hardening`):

```bash
cd /opt/caddy
cp compose.yml compose.yml.bak-$(date +%F)
# добавить в сервис caddy ключи read_only / tmpfs / cap_drop / cap_add /
# security_opt / pids_limit из deploy/caddy/compose.yml, затем:
docker compose config -q && docker compose up -d     # пересоздаст caddy: пауза в несколько секунд для всех сайтов хоста
docker compose ps caddy && docker compose logs --tail 50 caddy
curl -fsS https://gantt-ai-planner.duckdns.org/healthz
```

Если после пересоздания Caddy не стартует или какой-то сайт перестал
открываться (например, другой сайт хоста пишет на диск вне `/data`,
`/config`, `/tmp`) — вернуть `compose.yml.bak-…` и `docker compose up -d`.

## 2. Регулярный деплой

Обычный путь — через CD (`.github/workflows/deploy.yml`): после зелёного
CI на push в `main` GitHub Actions собирает образ, сканирует его Trivy
(падает на CRITICAL и HIGH, для которых есть исправленная версия) и
только после этого публикует в GHCR под тегами `sha-<short>` и `latest`
(публикуется ровно просканированный образ, без пересборки). Теги в GHCR
изменяемые, поэтому на сервер уходит не тег, а ссылка с digest, который
вернул этот `docker push`:

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i <deploy-key> deploy@<host> sha-<short>@sha256:<digest>
```

Forced command `planner-deploy-wrapper` проверяет, что пришёл ровно один
токен формата `^sha-[0-9a-f]{7,40}@sha256:[0-9a-f]{64}$` — **только с
digest**, голый тег `sha-<short>` отклоняется (в том числе для ручных
деплоев и откатов: тег изменяемый), — и вызывает
`sudo /usr/local/bin/planner-deploy <ref>`. `planner-deploy` проверяет то
же выражение ещё раз. Значение целиком попадает в `IMAGE_TAG` в `.env`,
Compose тянет `…:sha-<short>@sha256:<digest>` — ровно тот манифест, что
просканирован, даже если тег потом перепишут. Дальше `planner-deploy`:

1. берёт блокировку `/run/lock/planner-deploy.lock` (`flock -w 30`):
   одновременно идёт только один деплой (CD-запуски и так сериализованы
   `concurrency` в workflow, но ручной деплой/откат мог бы пересечься с
   ними). Если за 30 секунд блокировку взять не удалось — выход с кодом 1
   и сообщением `another deploy is running`, **ничего не тронуто** (даже
   `.env` не прочитан). Файл блокировки — только обычный файл root'а:
   симлинк или чужой файл в `/run/lock` скрипт не откроет;
2. проверяет, что каждый `file:`-секрет из `compose.prod.yml`
   (`secrets/…`) существует как обычный файл — иначе Docker молча создал
   бы на его месте пустой каталог и запустил контейнер с ним (например,
   если новый секрет появился в `compose.prod.yml` раньше, чем его
   сгенерировал `bootstrap.sh`); читает текущий `IMAGE_TAG` из `.env` —
   это цель отката — и проверяет его тем же регулярным выражением; если
   секрета нет, строки нет или значение не ссылка с digest (`latest`,
   голый тег, ручная правка, мусор), выходит с кодом 1, не вызывая docker
   вообще;
3. `IMAGE_TAG=<новый> timeout 300 docker compose pull app migrate` — ссылка
   передаётся только через окружение; если образа нет, сеть упала или
   pull не уложился в 300 секунд, скрипт выходит с кодом 1, **не трогая**
   `.env` и работающий стек;
4. снимает снапшот базы: `timeout 300 … pg_dump --lock-wait-timeout=60s
   -U planner_owner -Fc planner` в
   `/var/backups/gantt-planner/pre-deploy-<UTC-время>.dump` (0600; пишется
   во временный файл с уникальным именем `mktemp`
   `.pre-deploy.tmp.XXXXXXXX` и переименовывается; при совпадении секунды
   с прошлым снапшотом к имени добавляется суффикс; снапшоты старше 7 дней
   удаляются). `--lock-wait-timeout`: `pg_dump` сдаётся, а не ждёт
   бесконечно за чужой эксклюзивной блокировкой. Если дамп не удался,
   пустой или не уложился в 300 секунд — выход с кодом 1, **ничего не
   изменено**: без снапшота деплоя нет;
5. пишет новую ссылку в `.env` и делает `timeout 300 docker compose up -d`
   (это прогоняет `migrate` — `alembic upgrade head` от `planner_owner` —
   и перезапускает `app` на новом образе);
6. до 60 секунд опрашивает `/healthz` изнутри контейнера `app`
   (`docker compose exec app python -c ...`, каждая проба — под
   `timeout 20`);
7. если здоров — выходит с кодом 0;
8. при **любой** ошибке после записи `.env` (упал или не уложился в срок
   `up -d`, например `migrate` вышел с ошибкой; не прошёл healthcheck;
   скрипт прерван) — ловушка `EXIT` (единственный путь отката) возвращает
   в `.env` предыдущую ссылку и перезапускает **только приложение**:
   `timeout 300 docker compose up -d --no-deps app` — без `migrate`. Затем
   ещё раз проверяет `/healthz`, печатает, здоров ли откат, и имя
   снапшота из п. 4, и выходит с кодом 1 (CI увидит деплой как упавший).
   Так `.env` никогда не остаётся со ссылкой, которая не задеплоилась.
   Блокировка отпускается только после отката (когда процесс завершился).
   Поведение покрыто `bash deploy/tests/test_planner_deploy.sh`
   (docker, timeout и — где его нет, например в Git Bash — flock
   заглушены; удержанная блокировка проверяется настоящим `flock` там, где
   он есть).

Все сроки — `timeout -k 10 <сек>`: после SIGTERM через 10 секунд
следует SIGKILL. Худший случай для одного деплоя — около 30 + 300 + 300 +
300 + 60 секунд плюс откат; `ssh` из CD столько и подождёт. `timeout`
запускает команду в отдельной группе процессов (так по сроку
гарантированно убиваются и `docker`, и его плагин `compose`), поэтому при
ручном запуске в терминале Ctrl+C во время pull/dump/`up -d` может
сработать только после окончания шага или его срока.

Почему откат не запускает миграции: если новый релиз успел применить
миграцию, `alembic upgrade head` в образе предыдущей версии не знает
новую ревизию и падает («Can't locate revision»), а `app` зависит от
успешного `migrate` и не стартовал бы вовсе. Поэтому откат оставляет
схему как есть и поднимает старый код на новой схеме — это работает
только при соблюдении правила ниже.

После успешного деплоя CD дополнительно проверяет
`https://gantt-ai-planner.duckdns.org/healthz` снаружи.

### Гейты CI (до мёржа и до деплоя)

`Deploy` запускается только после зелёного `CI` на push в `main`; `CI`
на каждом PR и push проверяет, помимо тестов и линтеров:

- **pip-audit** — известные уязвимости во **всех** зафиксированных в
  `uv.lock` Python-зависимостях, включая dev (pytest, mypy, ruff…: они
  выполняются в CI с доступом к репозиторию);
- **npm audit --audit-level=high** — все npm-зависимости фронтенда,
  включая dev (сборщик, линтеры, Playwright), уровень high и выше;
- **Trivy по собранному образу** (job `e2e`, тот же образ, что гоняют
  e2e-тесты) и ещё раз в `Deploy` перед публикацией. Гейт образа
  покрывает **только исправимые HIGH/CRITICAL** CVE (`ignore-unfixed`):
  уязвимости без исправленной версии и MEDIUM/LOW не блокируют — иначе
  неисправимая CVE базового образа блокировала бы каждую сборку. Это не
  «в образе нет уязвимостей», а «нет уязвимостей, которые можно закрыть
  обновлением»;
- **N-1 проверка миграций** (ниже).

### Правило: миграции только обратно совместимые

Каждая миграция должна оставлять схему, на которой **предыдущий** релиз
продолжает работать (expand/contract):

- можно: новые таблицы, новые nullable-колонки или колонки с `DEFAULT`,
  новые индексы (для больших таблиц — `CREATE INDEX CONCURRENTLY`),
  новые значения в справочниках;
- нельзя в одном релизе с кодом, который это использует: удалять или
  переименовывать таблицы/колонки, менять тип колонки, добавлять
  `NOT NULL` без `DEFAULT`, ужесточать ограничения, которые старый код
  может нарушить;
- переименование/удаление — в два релиза: сначала код перестаёт
  использовать старое (и пишет в оба места, если нужно), и только в
  следующем релизе миграция удаляет старое;
- `downgrade()` по-прежнему пишется (CI гоняет `upgrade → downgrade base
  → upgrade`), но при откате он не вызывается.

**Правило проверяет CI** — шаги «N-1 migration check» в job `e2e`
(`.github/workflows/ci.yml`, логика в `scripts/migration-compat.sh`, его
можно запустить и локально: `bash scripts/migration-compat.sh <новый-образ>
<предыдущий-образ>`):

1. образ, собранный в этом запуске, прогоняет `alembic upgrade head` на
   чистом Postgres (роли как в проде — `deploy/initdb/10-roles.sh`,
   миграции от `planner_owner`);
2. на этой схеме запускается **предыдущий релиз** —
   `ghcr.io/lom911/gantt-ai-planner:sha-<первые 7 символов базового
   коммита>` (для PR — `base.sha`, для push — коммит до push'а, то есть
   то, что сейчас в проде) — от `planner_app`, с фейковой LLM;
3. smoke-тест старого приложения: `/healthz`, новая сессия,
   `GET /api/plan`, одна правка `POST /api/plan/operations`,
   `GET /api/plan/export`, один ход чата (пишет `chat_messages` и
   `chat_usage`; поток должен закончиться `done`), двойная выдача
   MCP-токена (`mcp_tokens`: отзыв + вставка) и удаление сессии (каскад по
   всем таблицам сессии, включая новые). Всё должно ответить 2xx.

Если образа базового коммита в GHCR нет (коммит не релизился), проверка
пропускается с notice, а не падает. Упавшая проверка значит: миграция
этого PR сломает прод при автооткате — переделать её по схеме
expand/contract (список выше). Для проверки самой проверки:
миграция `ADD COLUMN tokens integer NOT NULL` без `DEFAULT` в
`chat_usage` валит её на ходе чата (`500`, `NotNullViolation`), а
настоящая 0004 (новые таблицы, `tokens` с `DEFAULT 0`, частичный
уникальный индекс на `mcp_tokens`) проходит.

Если релиз с деструктивной миграцией всё же ушёл и данные повреждены —
восстановить снапшот, снятый перед ним (ниже).

### Где взять digest

Сервер принимает только `sha-<short>@sha256:<digest>`. Digest нужного
релиза:

- в workflow `Deploy` — в сводке запуска (Summary → «Image») и в логе шага
  «Push scanned image to GHCR» (`pushed ghcr.io/…:sha-<short>@sha256:…`);
- из реестра по тегу (с любой машины с Docker; пакет публичный):
  ```bash
  docker buildx imagetools inspect ghcr.io/lom911/gantt-ai-planner:sha-<short> --format '{{.Manifest.Digest}}'
  # или весь дескриптор манифеста: --format '{{json .Manifest}}'  (поле "digest")
  ```
  Это digest того, на что тег указывает **сейчас**; для релизов из CD он
  совпадает с тем, что напечатал `Deploy`, пока тег никто не переписал —
  при сомнениях брать значение из `Deploy`;
- для образа, который уже есть на сервере (например, предыдущий релиз):
  `docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/lom911/gantt-ai-planner:sha-<short>`
  (печатает `ghcr.io/…@sha256:<digest>`);
- ссылка предыдущего релиза целиком — в логе `planner-deploy`: перед
  каждым деплоем он печатает `previous: 'sha-…@sha256:…'` (вывод шага
  «Deploy over SSH» в Actions).

### Ручной деплой конкретного релиза (без CI)

Отдельного `scripts/deploy-manual.sh`, который упоминает спецификация
(§14), нет: его роль выполняют команды этого раздела и первого деплоя
(§1, п. 4) — `planner-deploy` уже делает блокировку, pull, снапшот,
запуск, healthcheck и откат.

```bash
ssh -i <deploy-key> deploy@<host> sha-<short>@sha256:<digest>
```

Или прямо на сервере от root (в обход wrapper'а, например для
диагностики; проверки те же):

```bash
sudo /usr/local/bin/planner-deploy sha-<short>@sha256:<digest>
```

### Откат

Откат на релиз **без миграций между ним и текущим** — это обычный деплой
предыдущего релиза по его ссылке с digest (см. «Где взять digest»):

```bash
ssh -i <deploy-key> deploy@<host> sha-<предыдущий-short>@sha256:<digest>
```

`planner-deploy` откатывается автоматически при неуспешном healthcheck;
ручной откат нужен, если проблема обнаружилась позже (например, в логах
или у пользователей), а не сразу при деплое.

Если после целевого тега были миграции, обычный деплой старого тега не
пройдёт: его `migrate` упадёт на неизвестной ревизии, и `planner-deploy`
сам вернётся на текущий тег. Тогда откатывать только код, без
`migrate`, вручную от root (схема обратно совместима по правилу выше;
блокировка — та же, что у `planner-deploy`, чтобы не пересечься с CD):

```bash
cd /opt/gantt-planner
old='sha-<предыдущий-short>@sha256:<digest>'    # только с digest: иначе следующий деплой откажется
[[ "$old" =~ ^sha-[0-9a-f]{7,40}@sha256:[0-9a-f]{64}$ ]] || echo "не ссылка с digest!"
exec 9>/run/lock/planner-deploy.lock; flock -w 30 9 || echo "идёт деплой — дальше не продолжать, повторить позже"
IMAGE_TAG="$old" timeout 300 docker compose -f compose.prod.yml pull app
sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=$old|" .env
timeout 300 docker compose -f compose.prod.yml up -d --no-deps app
docker compose -f compose.prod.yml ps app   # дождаться (healthy)
exec 9>&-                                      # отпустить блокировку
```

Пока в `.env` стоит тег старше схемы, любые перезапуски `app` делать с
`--no-deps` (как в разделе 3): без него Compose заодно запустит
`migrate` старого образа, тот упадёт, и `app` не поднимется. Следующий
нормальный деплой (новее схемы) снимает это ограничение.

### Восстановление снапшота перед деплоем

Снапшоты `pre-deploy-<UTC>.dump` лежат рядом с ночными дампами
(`ls -lt /var/backups/gantt-planner/`); имя снапшота конкретного деплоя
`planner-deploy` печатает в лог (и повторяет при автооткате). Это
полный `pg_dump -Fc` базы `planner`, восстановление **заменяет** базу
целиком: всё, что пользователи записали после снапшота, пропадёт.
Сначала проверить дамп на scratch-базе (раздел 4), потом:

```bash
cd /opt/gantt-planner
snap=/var/backups/gantt-planner/pre-deploy-<UTC>.dump

# 1. остановить приложение (даунтайм до п. 5)
docker compose -f compose.prod.yml stop app

# 2. на всякий случай — дамп текущего состояния
( umask 077 && docker compose -f compose.prod.yml exec -T db \
    pg_dump -U planner_owner -Fc planner > /var/backups/gantt-planner/before-restore-$(date -u +%Y%m%dT%H%M%SZ).dump )

# 3. пересоздать базу: объекты, созданные после снапшота (таблицы новых
#    миграций), тоже исчезнут, и ревизия alembic вернётся к снапшотной
docker compose -f compose.prod.yml exec -T db psql -U postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE planner WITH (FORCE);" \
  -c "CREATE DATABASE planner OWNER planner_owner;"

# 4. восстановить (владельцы, GRANT'ы для planner_app и default privileges
#    восстанавливаются из дампа)
docker compose -f compose.prod.yml exec -T db pg_restore -U postgres -d planner --exit-on-error < "$snap"

# 5. вернуть ссылку, которая работала с этой схемой (та, что была до
#    неудачного деплоя: `previous: '…'` в его логе), и поднять только приложение
sed -i 's|^IMAGE_TAG=.*|IMAGE_TAG=sha-<тег-до-деплоя>@sha256:<digest>|' .env
docker compose -f compose.prod.yml up -d --no-deps app
curl -fsS https://gantt-ai-planner.duckdns.org/healthz
```

Процедура проверена на копии стека: после восстановления таблицы
принадлежат `planner_owner`, у `planner_app` снова только DML, default
privileges на месте, приложение пишет в базу.

## 3. Ротация секретов

При любой ротации: сначала положить новое значение, затем перезапустить
только те контейнеры, которым оно нужно (secrets в Compose читаются при
старте контейнера, hot reload не поддерживается). `app` перезапускается
с `--no-deps`: иначе Compose заодно заново запустит `migrate`, а после
отката на тег старше схемы (раздел 2, «Откат») тот упадёт и `app` не
поднимется.

### Ключ Anthropic

```bash
# Ключ вводится без эха и не попадает ни в историю shell, ни в аргументы команд.
read -rs -p 'Новый ключ Anthropic: ' key && printf '%s' "$key" > /opt/gantt-planner/secrets/anthropic_api_key; unset key
cd /opt/gantt-planner && docker compose -f compose.prod.yml up -d --no-deps --force-recreate app
```
Старый ключ отозвать в консоли Anthropic после подтверждения, что новый
работает.

### Ключ OpenRouter

По умолчанию (`LLM_PROVIDER=openrouter`) приложение использует именно этот
секрет. Тот же принцип, что и для Anthropic (ключ не должен попасть ни в
историю shell, ни в аргументы команд, ни на экран) — здесь как
альтернативный вариант через `install` и `/dev/stdin`, без переменной
окружения и без `read`:

```bash
cd /opt/gantt-planner
install -m 0444 -o root -g root /dev/stdin secrets/openrouter_api_key
# Вставить ключ (sk-or-v1-...) одной строкой без завершающего перевода
# строки и нажать Ctrl+D (EOF). Ctrl+C прервёт без изменения файла.
docker compose -f compose.prod.yml up -d --no-deps --force-recreate app
```
Старый ключ отозвать в личном кабинете OpenRouter после подтверждения, что
новый работает. Тот же приём (`install -m 0444 -o root -g root /dev/stdin
<файл>` + вставка + Ctrl+D) годится и для `secrets/anthropic_api_key`
вместо `read -rs` выше — оба способа не оставляют ключ в истории shell.

### Переключение провайдера LLM

Провайдер и модель заданы в `/opt/gantt-planner/.env` (`LLM_PROVIDER`,
`LLM_MODEL`; читает их `compose.prod.yml` через `${LLM_PROVIDER:-openrouter}`
/ `${LLM_MODEL:-anthropic/claude-sonnet-5}` — bootstrap прописывает эти
значения по умолчанию при первом создании файла и не трогает `.env`, если
он уже существует). Чтобы переключиться:

```bash
cd /opt/gantt-planner
# Anthropic -> OpenRouter (или наоборот) — отредактировать .env,
# например через sed, задав нужные значения:
sed -i \
  -e 's/^LLM_PROVIDER=.*/LLM_PROVIDER=openrouter/' \
  -e 's/^LLM_MODEL=.*/LLM_MODEL=anthropic\/claude-sonnet-5/' \
  .env
docker compose -f compose.prod.yml up -d --no-deps --force-recreate app
```
Убедиться, что соответствующий секрет (`secrets/openrouter_api_key` или
`secrets/anthropic_api_key`) уже заполнен — иначе приложение молча уйдёт в
демо-режим (см. раздел 1, п. 3). Текущий провайдер и модель видны в
`GET /api/meta` (`llm_mode`, `model`) сразу после переключения.

Отдельно поддержан и автоопределение: если в `secrets/anthropic_api_key`
случайно оказался ключ OpenRouter (начинается с `sk-or-`) при
`LLM_PROVIDER=anthropic`, приложение всё равно пойдёт через OpenRouter и
один раз залогирует предупреждение (без содержимого ключа) — специально
переключать `.env` в этом случае не обязательно, но лучше всё же явно
выставить `LLM_PROVIDER=openrouter`, чтобы не полагаться на автоопределение.

### Пароли БД (`db_app_password`, `db_owner_password`, `pg_superuser_password`)

Файл секрета — это то, что подставляется при следующем старте
контейнера; сам пароль в Postgres нужно поменять отдельно командой
`ALTER ROLE`, иначе роль и файл разойдутся.

Новый пароль нигде не должен оказаться в открытом виде: ни в аргументах
команд (их видно в `ps`), ни в истории shell, ни в логе Postgres. Поэтому
он сразу пишется в файл, а в `psql` попадает только через stdin:

```bash
cd /opt/gantt-planner

# Пример для planner_app; для planner_owner — аналогично, с его ролью/файлом.
( umask 077 && openssl rand -base64 32 | tr -d '\n' > secrets/db_app_password.new )

# printf — встроенная команда bash (не отдельный процесс), так что пароль
# идёт только через pipe; SET выключает запись текста запроса в лог
# сервера, если ALTER вдруг упадёт. В base64 нет кавычек.
{
  echo "SET log_min_error_statement = panic;"
  printf "ALTER ROLE planner_app PASSWORD '%s';\n" "$(cat secrets/db_app_password.new)"
} | docker compose -f compose.prod.yml exec -T db psql -U postgres -v ON_ERROR_STOP=1 -q

mv secrets/db_app_password.new secrets/db_app_password
chmod 0444 secrets/db_app_password

docker compose -f compose.prod.yml up -d --no-deps --force-recreate app
```

Если ALTER упал, `.new`-файл остаётся, а рабочий пароль не меняется —
разобраться с ошибкой и повторить.

Для `pg_superuser_password` (переменная `POSTGRES_PASSWORD_FILE`) — то же
самое с `ALTER ROLE postgres` и файлом `secrets/pg_superuser_password`,
затем перезапустить `db` (это вызовет короткий даунтайм — сделать вне
пиковых часов).

Ключевой момент: **никогда не менять только файл секрета** — контейнер
Postgres создаёт роли один раз, при первом старте на пустом томе;
изменение файла без `ALTER ROLE` в уже существующей базе ничего не даст.

### Токен ops-эндпоинта (`ops_token` / `OPS_TOKEN`)

Им workflow `Uptime` читает `GET /api/ops/status`. Ротация (короткое окно,
когда Uptime видит `401` и может открыть issue — оно закроется само на
следующей проверке):

```bash
cd /opt/gantt-planner
( umask 177 && openssl rand -hex 32 > secrets/ops_token.new ) && [ -s secrets/ops_token.new ] \
  && chmod 0444 secrets/ops_token.new && mv secrets/ops_token.new secrets/ops_token
docker compose -f compose.prod.yml up -d --no-deps --force-recreate app
cat secrets/ops_token    # -> GitHub: Settings → Secrets and variables → Actions → OPS_TOKEN → Update
```

### Deploy-ключ (SSH)

1. Сгенерировать новую пару ключей (`ssh-keygen -t ed25519 -C
   gha-gantt-planner`).
2. Добавить новую публичную часть в
   `/home/deploy/.ssh/authorized_keys` (со `restrict,command="..."`),
   старую строку не удалять пока не убедились, что новая работает.
3. Обновить секрет `DEPLOY_SSH_KEY` (приватный ключ) в GitHub Environment
   `production`.
4. Прогнать `Deploy` через `workflow_dispatch`, убедиться, что деплой
   прошёл.
5. Удалить старую строку из `authorized_keys`.

## 4. Проверка восстановления из бэкапа

Бэкап делает `deploy/backup.sh` каждую ночь в 03:15
(`/etc/cron.d/gantt-planner-backup`): `pg_dump -Fc` от `planner_owner` в
`/var/backups/gantt-planner/<дата>.dump`, дампы старше 7 дней удаляются.
Пустой вывод `pg_dump` считается ошибкой и не затирает хороший дамп.

Сбой бэкапа не проходит молча — каждый запуск оставляет след:

```bash
journalctl -t gantt-planner-backup --since -2d   # "ok: wrote ..." или "FAILED (exit N) ..."
cat /var/lib/gantt-planner/backup-status          # итог последнего запуска
tail -n 50 /var/log/gantt-planner-backup.log      # полный вывод cron (stderr pg_dump)
```

`backup-status` — пары `ключ=значение`: `timestamp` (UTC), `status`
(`ok`/`fail`), `exit_code`, `size_bytes`, `file` и `last_ok` — время
последнего успешного бэкапа, сохраняется и после неудачных запусков
(видно, насколько устарел свежайший хороший дамп). Приложение читает
этот файл (каталог `/var/lib/gantt-planner` смонтирован в `app` только
для чтения) и отдаёт итог в `GET /api/ops/status`; workflow `Uptime`
поднимает алерт, если бэкап упал, его статус неизвестен или свежайшему
хорошему дампу больше 30 часов (раздел 6).

Снапшоты `pre-deploy-*.dump` (раздел 2) лежат в том же каталоге и
годятся для восстановления так же, как ночные.

Восстановление проверяется руками, в отдельную (scratch) базу, **не** в
`planner` — чтобы не задеть продакшен-данные:

```bash
cd /opt/gantt-planner

# 1. создать временную базу и роль-владельца в том же контейнере db
docker compose -f compose.prod.yml exec -T db psql -U postgres -c \
  "CREATE DATABASE planner_restore_test OWNER planner_owner;"

# 2. восстановить последний дамп в неё
latest_dump="$(ls -1t /var/backups/gantt-planner/*.dump | head -n1)"
docker compose -f compose.prod.yml exec -T db pg_restore \
  -U planner_owner -d planner_restore_test --no-owner < "$latest_dump"

# 3. проверить, что данные на месте (пример)
docker compose -f compose.prod.yml exec -T db psql -U planner_owner \
  -d planner_restore_test -c "SELECT count(*) FROM plan_versions;"

# 4. убрать за собой
docker compose -f compose.prod.yml exec -T db psql -U postgres -c \
  "DROP DATABASE planner_restore_test;"
```

Дата и результат последней проверки восстановления фиксируются здесь:

| Дата | Кто проверял | Результат |
|---|---|---|
| _(заполнить после первой проверки)_ | | |

### Офсайт-копии (зашифрованные, в приватный репозиторий GitHub)

Локальные дампы лежат на том же диске, что и база: при потере сервера
они пропадут вместе с ним. Поэтому после каждого успешного ночного дампа
`backup.sh` запускает `/usr/local/bin/gantt-planner-offsite-backup.sh`
(`deploy/offsite-backup.sh`), который:

1. шифрует дамп **только публичным** ключом:
   `openssl cms -encrypt -binary -aes-256-cbc -outform DER -in <дамп> -out <дамп>.cms /etc/gantt-planner/backup-recipient.pem`
   — приватного ключа на сервере нет, поэтому ни сервер (даже
   взломанный), ни GitHub прочитать копии не могут;
2. кладёт `dumps/<ГГГГ-ММ-ДД>.dump.cms` в **приватный** репозиторий
   GitHub по SSH с deploy-ключом с правом записи
   (`GIT_SSH_COMMAND='ssh -i /etc/gantt-planner/backup-deploy-key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/gantt-planner/github_known_hosts …'`);
   адрес и ветка — из `/etc/gantt-planner/offsite.conf`;
3. оставляет в дереве 30 самых новых копий (по имени = дате; старые
   остаются в истории git);
4. пишет в syslog (`journalctl -t gantt-planner-offsite`) и в
   `backup-status` строки `offsite_status` (`ok` / `fail` /
   `not_configured`), `offsite_last_ok`, `offsite_file`.

Пока `offsite.conf`, сертификат или deploy-ключ не созданы, скрипт пишет
`offsite not configured` и выходит с кодом 0 — локальный бэкап всё равно
считается успешным. Сбой офсайт-копии тоже не делает ночной бэкап
неуспешным (он записан как `offsite_status=fail` и предупреждение в
журнале `gantt-planner-backup`); застой ловит проверка восстановления
(ниже): она падает, если свежайшей копии больше 3 дней. Платного
хранилища не нужно: приватный репозиторий и Actions (≈2 минуты в неделю
из бесплатных 2000 в месяц) бесплатны.

Где что лежит:

| Файл | Где | Примечание |
|---|---|---|
| `backup-private.pem` (приватный ключ) | секрет `BACKUP_PRIVATE_KEY` репозитория бэкапов **и** офлайн-копия (менеджер паролей / зашифрованный носитель) | **никогда** не на сервере; без него копии не расшифровать |
| `backup-recipient.pem` (сертификат, публичный) | `/etc/gantt-planner/backup-recipient.pem`, 0644 | срок — 10 лет |
| `backup-deploy-key` (приватный SSH-ключ) | `/etc/gantt-planner/backup-deploy-key`, 0600 | создаётся на сервере и его не покидает |
| `backup-deploy-key.pub` | репозиторий бэкапов → Settings → Deploy keys, **Allow write access** | |
| `offsite.conf` | `/etc/gantt-planner/offsite.conf`, 0600 | образец — `deploy/offsite/offsite.conf.example` |
| `github_known_hosts` | `/etc/gantt-planner/github_known_hosts` | ставит bootstrap (`deploy/offsite/github_known_hosts`) |
| `restore-check.yml` | репозиторий бэкапов → `.github/workflows/restore-check.yml` | копия `deploy/offsite/restore-check.yml` |

Настройка (один раз, по порядку):

1. **На своей машине** (не на сервере) — ключ и сертификат:
   ```bash
   openssl req -x509 -newkey rsa:4096 -nodes -keyout backup-private.pem -out backup-recipient.pem -days 3650 -subj /CN=gantt-planner-backups
   ```
   `backup-private.pem` сразу убрать в менеджер паролей / на
   зашифрованный носитель.
2. Создать на GitHub **приватный** репозиторий (например,
   `gantt-ai-planner-backups`, с README, чтобы появилась ветка `main`);
   добавить в него `.github/workflows/restore-check.yml` из
   `deploy/offsite/restore-check.yml`; Settings → Secrets and variables →
   Actions → New repository secret `BACKUP_PRIVATE_KEY` = всё содержимое
   `backup-private.pem` (с строками `-----BEGIN/END PRIVATE KEY-----`);
   подписаться на issues репозитория (Watch → Custom → Issues).
3. На сервере — `sudo bash deploy/bootstrap.sh` (ставит скрипт,
   `/etc/gantt-planner` и `github_known_hosts`) и проверить отпечаток
   ключа github.com — должен быть
   `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`, как на
   docs.github.com («GitHub's SSH key fingerprints»):
   ```bash
   ssh-keygen -lf /etc/gantt-planner/github_known_hosts
   ```
4. Сертификат (скопировать `backup-recipient.pem` на сервер, например
   `scp`, затем):
   ```bash
   sudo install -m 0644 -o root -g root backup-recipient.pem /etc/gantt-planner/backup-recipient.pem
   ```
5. Deploy-ключ — на сервере, и его публичную часть в репозиторий бэкапов
   (Settings → Deploy keys → Add deploy key → **Allow write access**):
   ```bash
   sudo ssh-keygen -t ed25519 -N '' -C gantt-planner-offsite -f /etc/gantt-planner/backup-deploy-key
   sudo cat /etc/gantt-planner/backup-deploy-key.pub
   ```
6. Настройки:
   ```bash
   sudo install -m 0600 -o root -g root deploy/offsite/offsite.conf.example /etc/gantt-planner/offsite.conf
   sudo sed -i 's|^OFFSITE_REPO=.*|OFFSITE_REPO=git@github.com:<owner>/gantt-ai-planner-backups.git|' /etc/gantt-planner/offsite.conf
   ```
7. Проверить сразу, не дожидаясь ночи (берёт последний успешный ночной
   дамп из `backup-status`):
   ```bash
   sudo /usr/local/bin/gantt-planner-offsite-backup.sh
   journalctl -t gantt-planner-offsite --since -10min
   grep '^offsite_' /var/lib/gantt-planner/backup-status
   ```
   В репозитории появится коммит `backup <дата>.dump`. Затем в
   репозитории бэкапов Actions → Restore check → Run workflow — должен
   пройти (в сводке: число строк `plan_versions`, ревизия alembic).

Проверка восстановления (`restore-check.yml` в репозитории бэкапов,
раз в неделю и вручную): расшифровывает свежайший `dumps/*.cms`
секретом `BACKUP_PRIVATE_KEY`, создаёт роли `planner_owner`/`planner_app`
и базу `planner` в сервисе `postgres:17-alpine` (тот же образ, что в
проде), восстанавливает `pg_restore --exit-on-error` и проверяет, что
есть таблицы `sessions`, `plan_versions`, `chat_messages`,
`alembic_version`, что `select count(*) from plan_versions` выполняется,
что в `alembic_version` есть ревизия и что копии не больше 3 дней. При
провале — issue с меткой `restore-check` в репозитории бэкапов (и
упавший запуск), при успехе issue закрывается. Вся цепочка (реальный
`openssl cms` → push → этот шаг workflow → `pg_restore`, плюс
неправильный ключ, устаревшая копия, пустой секрет) проверена локально
на одноразовых контейнерах.

Восстановление вручную (сервер потерян или нужен конкретный день) — на
машине с приватным ключом:

```bash
git clone git@github.com:<owner>/gantt-ai-planner-backups.git && cd gantt-ai-planner-backups
git log --oneline -- dumps/          # нужный день; старше 30 дней — в истории: git checkout <commit> -- dumps/<день>.dump.cms
openssl cms -decrypt -binary -inform DER -in dumps/<ГГГГ-ММ-ДД>.dump.cms -inkey backup-private.pem -out planner.dump
```

Дальше `planner.dump` — обычный `pg_dump -Fc`: скопировать на сервер
(`scp`, 0600) и восстановить как снапшот (раздел 2, «Восстановление
снапшота перед деплоем», п. 1–5) или сначала в scratch-базу (выше в этом
разделе). На **новом** сервере: `bootstrap.sh`, секреты, первый деплой
(раздел 1) — роли создаст `initdb` на пустом томе, — затем то же
восстановление. После — удалить расшифрованный файл (`shred -u
planner.dump`).

Ограничения: deploy-ключ с правом записи позволяет взломанному серверу
удалить копии или переписать историю (`push --force`); защита веток в
бесплатных приватных репозиториях недоступна, поэтому офлайн-копия
приватного ключа и еженедельная проверка — обязательная часть схемы, а
при подозрении на взлом сервера — сразу удалить deploy-ключ в
репозитории бэкапов (раздел 5). GitHub не принимает файлы больше 100 МБ
(скрипт отказывается от копий больше 95 МБ — тогда нужно другое
хранилище), а история репозитория растёт на размер одной копии в день.

Сроки хранения: удалённые пользователем данные остаются в офсайт-копиях
(зашифрованными) 30 дней в дереве и **дольше — в истории git**, пока её не
сократить. Чтобы история не держала копии старше дерева (и не росла),
время от времени — например, раз в квартал — схлопывать её в один коммит
(на машине с доступом к репозиторию бэкапов; следующий ночной push
продолжит от него):

```bash
git clone git@github.com:<owner>/gantt-ai-planner-backups.git && cd gantt-ai-planner-backups
git checkout --orphan squashed && git commit -q -m "squash backup history" && git push --force origin squashed:main
```

GitHub ещё какое-то время хранит недостижимые объекты у себя; для
гарантированного удаления — запрос в поддержку GitHub.

## 5. Чек-лист при инциденте (компрометация секрета/сервера)

Если есть подозрение, что скомпрометирован ключ Anthropic, пароль БД,
deploy-ключ или сам сервер — отзываем всё сразу, не по одному:

1. **Ключ LLM (Anthropic или OpenRouter, смотря какой активен —
   `GET /api/meta`)**: отключить ключ в консоли соответствующего сервиса
   (лимит расходов на нуле или удаление ключа), сгенерировать новый,
   положить по процедуре из раздела 3.
2. **Пароли БД**: сменить все три (`planner_app`, `planner_owner`,
   суперпользователь) по процедуре из раздела 3, даже если под
   подозрением только один.
3. **Deploy-ключ**: удалить скомпрометированную строку из
   `/home/deploy/.ssh/authorized_keys` немедленно (это отключает CD),
   затем выпустить новый ключ по процедуре из раздела 3. Если под
   подозрением сервер — ещё и удалить deploy-ключ офсайт-бэкапов в
   репозитории бэкапов (Settings → Deploy keys), чтобы с сервера нельзя
   было стереть копии, и выпустить новый (раздел 4, «Офсайт-копии», п. 5);
   сами копии зашифрованы ключом, которого на сервере нет.
4. **GitHub**: проверить `DEPLOY_SSH_KEY`/`DEPLOY_KNOWN_HOSTS` в
   Environment `production`, при необходимости отозвать и пересоздать
   `GITHUB_TOKEN`-зависимые интеграции (пакет в GHCR публичный, но права
   на публикацию идут через сам workflow). Если под подозрением сервер —
   сменить и `ops_token`/`OPS_TOKEN` (раздел 3).
5. **Сессии пользователей**: если есть подозрение на утечку данных
   планов — в куке `__Host-sid` лежит случайный непрозрачный токен
   (256 бит), а в БД хранится только его sha256 (`sessions.token_hash`),
   так что дамп БД сам по себе не даёт войти в чужую сессию.
   Инвалидировать сессии можно, удалив строки из таблицы `sessions`
   через `psql` в контейнере `db` (каскадно удалятся версии планов, чат
   и MCP-токены).
6. Зафиксировать инцидент: что произошло, что отозвано и когда, что
   проверено после — как дополнение к этому разделу или в отдельном
   issue.
7. Только после того, как все новые секреты на месте и стек передеплоен
   — считать инцидент закрытым.

## 6. Мониторинг доступности, метрик и алерты

`.github/workflows/uptime.yml` раз в 15 минут (и вручную — Actions →
Uptime → Run workflow) проверяет прод снаружи, с раннера GitHub:

- `GET https://gantt-ai-planner.duckdns.org/healthz` — `200` и
  `{"status":"ok"}` (приложение живо и достаёт до базы);
- `GET /api/meta` — `200` и `llm_mode` не `fake`: `fake` на проде значит,
  что ключ LLM пустой или не прочитался (раздел 1, п. 3; раздел 3);
- `GET /api/ops/status` с заголовком `Authorization: Bearer <OPS_TOKEN>`
  (секрет репозитория `OPS_TOKEN` = `/opt/gantt-planner/secrets/ops_token`)
  — внутренние метрики приложения. Ответ:
  `{"window_minutes":15,"requests":…,"errors_5xx":…,"error_rate":…,"p95_ms":…,"tokens_today":…,"chat_messages_today":…,"disk_free_ratio":…,"backup":{"status":"ok|fail|unknown","last_ok":…,"age_hours":…}}`.
  Алерт, если:

  | условие | смысл |
  |---|---|
  | `requests >= 20` и `error_rate > 0.05` | больше 5 % ответов 5xx за последние 15 минут (при малом трафике не считается) |
  | `p95_ms > 3000` | p95 задержки за 15 минут больше 3 секунд |
  | `tokens_today > 3000000` | за сутки потрачено больше 3 млн токенов LLM (расходы) |
  | `disk_free_ratio < 0.10` | свободно меньше 10 % диска |
  | `backup.status == "fail"` | последний ночной бэкап упал |
  | `backup.status == "unknown"` | приложение не может прочитать `backup-status` |
  | `backup.age_hours > 30` | свежайшему хорошему дампу больше 30 часов |

  А также `401/403` (секрет `OPS_TOKEN` не совпадает с файлом на
  сервере), другой код или тело не того формата. Без секрета `OPS_TOKEN`
  проверка метрик пропускается с notice; `404` значит, что эндпоинт ещё
  не задеплоен, — тоже notice, не алерт.

Проверка `/healthz` и `/api/meta` повторяется до 3 раз с паузой 20
секунд, запрос метрик — до 3 раз при сетевой ошибке или 5xx, так что
разовый сбой сети алерта не вызывает. Если проверка не прошла:

- открывается issue с меткой `uptime` (метка создаётся автоматически),
  а если такой issue уже открыт — в него добавляется комментарий с
  результатом очередной проверки (то есть во время долгого простоя —
  раз в ~15 минут);
- запуск workflow помечается упавшим — GitHub присылает письмо о падении
  scheduled-workflow (тому, кто последним менял cron в файле).

Когда проверка снова проходит, workflow закрывает открытые issue
`uptime` с комментарием о восстановлении. Чтобы получать алерты, нужно
следить за репозиторием (Watch → Custom → Issues) или за письмами об
упавших workflow.

Что делать при алерте:

1. Открыть issue — там причина с последней попытки (код ответа и
   начало тела) и ссылка на запуск.
2. `healthz` не `200` / таймаут: на сервере
   `cd /opt/gantt-planner && docker compose -f compose.prod.yml ps` и
   `logs --tail 200 app db`; проверить Caddy
   (`cd /opt/caddy && docker compose ps && docker compose logs --tail 100`)
   и что он в сетях `edge` и `planner-proxy`. Если сломал последний
   деплой — откат (раздел 2).
3. `llm_mode` = `fake`: заполнить ключ LLM (раздел 3) и перезапустить
   `app`.
4. Метрики из `/api/ops/status`:
   - 5xx / p95 — `docker compose -f compose.prod.yml logs --since 30m app`
     (ошибки, медленные запросы), нагрузка на хост (`docker stats`,
     `uptime`); если началось с деплоя — откат (раздел 2);
   - токены — кто тратит: `GET /api/meta`, логи чата; при подозрении на
     злоупотребление — снизить лимиты (`CHAT_LIMIT_PER_DAY` и др.) или
     временно отключить ключ LLM (раздел 5, п. 1);
   - диск — `df -h /`, `du -sh /var/backups/gantt-planner
     /var/lib/gantt-planner/pg /var/lib/docker`; старые образы:
     `docker image prune` (осторожно: общий хост — только свои образы);
   - бэкап — раздел 4 (`journalctl -t gantt-planner-backup`,
     `cat /var/lib/gantt-planner/backup-status`); `unknown` — файла нет
     или он не смонтирован в `app` (`docker compose … exec app cat
     /var/lib/gantt-planner/backup-status`);
   - `401/403` — секрет `OPS_TOKEN` в GitHub не совпадает с
     `secrets/ops_token` (раздел 3, «Токен ops-эндпоинта»).
5. Issue закроется сам на следующей успешной проверке (или запустить
   Uptime вручную).

Ограничения: GitHub запускает schedule только из ветки по умолчанию, с
задержкой до десятков минут при нагрузке, и отключает его после 60 дней
без активности в репозитории (включить обратно: Actions → Uptime →
Enable workflow). Метрики — то, что приложение само посчитало за
последние 15 минут в своём процессе, раз в 15 минут: короткий всплеск
между проверками может остаться незамеченным, а если приложение лежит,
метрик нет вовсе (тогда алерт даёт `/healthz`).

### Еженедельные eval-прогоны на живой LLM

`.github/workflows/evals.yml` раз в неделю (понедельник, 06:23 UTC) и
вручную (Actions → Evals → Run workflow) запускает
`cd backend && uv run python -m evals.run --base-url https://gantt-ai-planner.duckdns.org`
— сценарии чата из `backend/evals` против прода с настоящей моделью
(регрессии промпта, инструментов, смены модели, которых не видят тесты с
фейковой LLM). Алерт — так же, как у Uptime: при провале открывается (или
дополняется комментарием) issue с меткой `evals` с таблицей PASS/FAIL, а
запуск помечается упавшим; при успехе открытые issue `evals` закрываются.

Каждый прогон тратит токены LLM и лимиты прода: 6 новых сессий и 7
сообщений чата с адреса раннера (лимит — 20 сессий в час на IP) плюс
общая дневная квота чата, поэтому чаще раза в неделю по расписанию не
запускать. Формулировки модели от прогона к прогону разные: перед тем как
чинить приложение, повторить упавший сценарий локально с `--only NAME -v`
(`backend/evals/README.md`).
