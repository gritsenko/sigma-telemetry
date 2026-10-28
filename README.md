# sigma-telemetry

Лёгкий сборщик анонимной статистики для статической веб-игры (Sigma Ruler на GitHub Pages): сессии, активное время, события воронки. Один контейнер FastAPI + SQLite, HTTPS через Caddy. Схема приёма повторяет ext-play из jam.my.games, код написан заново.

## Как это работает

- **Клиент** (`client/telemetry.ts`) — ~1 КБ gzip, без зависимостей. Копируется в игру.
  - Анонимный `client` в localStorage, новый `session` на каждую загрузку страницы.
  - Считает только время, когда вкладка видна и в фокусе.
  - События копятся в очереди и уходят раз в 20 с игры или по 20 штук. При уходе со страницы — `sendBeacon`.
  - Тело — JSON строкой (`text/plain`): простой CORS-запрос без preflight и без куки.
  - Если браузер не принял beacon, пачка сохраняется и уходит при следующей загрузке.
  - `setEnabled(false)` выключает отправку и запоминает выбор.
- **Сервер** (`server/app.py`) принимает `POST /e`. Он ничему не верит:
  - белый список Origin и лимит тела 16 КБ;
  - token bucket на IP;
  - сессия привязана к клиенту, который её открыл;
  - активное время только растёт и не обгоняет реальное время больше чем на 30 с;
  - до 50 событий за запрос и 1000 на сессию, до 200 разных имён (дальше — `_other`);
  - `params` до 1 КБ.

Формат запроса:

```json
{ "client": "<16-64 [A-Za-z0-9_-]>", "session": "<то же>", "build": "abc123", "lang": "ru-RU",
  "activeMs": 61000, "ended": false,
  "events": [{ "name": "city_founded", "at": 53120, "params": { "turn": 3 } }] }
```

`at` — миллисекунды от загрузки страницы. Имена событий приводятся к `[a-z0-9_]{1,64}`.

## Развёртывание

Сначала DNS: A-запись `sigmastat.gritsenko.biz` на внешний IP. Порты 80 и 443 должны доходить до машины с Caddy: если контейнер за NAT хоста, пробросьте их. Через порт 80 Caddy получает сертификат.

### LXC-контейнер (Debian/Ubuntu, без Docker)

```bash
git clone https://github.com/gritsenko/sigma-telemetry.git /opt/sigma-telemetry
/opt/sigma-telemetry/deploy/install.sh        # от root
cat /etc/sigma-telemetry.env                  # STATS_TOKEN для чтения статистики
curl https://sigmastat.gritsenko.biz/health   # {"ok":true}
```

Скрипт ставит `python3-venv`, `caddy` и `sqlite3`, заводит пользователя `sigmastat` и venv в `/opt/sigma-telemetry/.venv`. Затем поднимает systemd-сервис `sigma-telemetry` на `127.0.0.1:8000` и добавляет сайт в `/etc/caddy/Caddyfile`.

- База: `/var/lib/sigma-telemetry/telemetry.db`.
- Настройки: `/etc/sigma-telemetry.env`; после правки — `systemctl restart sigma-telemetry`.
- Логи: `journalctl -u sigma-telemetry -f`.
- Обновление: `cd /opt/sigma-telemetry && git pull && deploy/install.sh`.
- Другой домен: `DOMAIN=… deploy/install.sh`.

Если TLS уже снимает reverse proxy на хосте, Caddy в контейнере не нужен: направьте прокси на `<IP контейнера>:8000`. Сервис по умолчанию слушает только 127.0.0.1, поэтому поменяйте `--host` в unit-файле. И передавайте `X-Forwarded-For`: по нему считается лимит частоты.

### Docker

Внутри LXC Docker работает только с включённым nesting (в Proxmox: `features: nesting=1,keyctl=1`).

1. На машине:
   ```bash
   git clone https://github.com/gritsenko/sigma-telemetry.git && cd sigma-telemetry
   cp .env.example .env    # DOMAIN, ALLOWED_ORIGINS, STATS_TOKEN (openssl rand -hex 24)
   docker compose up -d --build
   curl https://$DOMAIN/health   # {"ok":true}
   ```
   Caddy сам получит сертификат Let's Encrypt. База лежит в `./data/telemetry.db`, бэкап — копия этого файла (лучше `sqlite3 data/telemetry.db ".backup b.db"`).
2. Обновление: `git pull && docker compose up -d --build`.

### В игре

`createTelemetry({ endpoint: 'https://sigmastat.gritsenko.biz/e', build })`.

## Чтение статистики

Все запросы требуют `Authorization: Bearer $STATS_TOKEN` (или `?token=`). `days` по умолчанию 30.

| Запрос | Что отдаёт |
|---|---|
| `GET /stats/summary?days=30` | по дням (UTC): сессии, уникальные и новые клиенты, активные минуты |
| `GET /stats/events?days=30&build=abc` | по каждому событию: число, сессии, клиенты |
| `GET /stats/funnel?steps=app_open,game_start,city_founded` | сколько клиентов прошло шаги по порядку |
| `GET /stats/params?name=civ_chosen&key=civ` | распределение значений одного поля `params` |

```bash
curl -s -H "authorization: Bearer $STATS_TOKEN" "https://sigmastat.gritsenko.biz/stats/funnel?steps=app_open,game_start,city_founded" | jq
```

Для произвольных запросов: `sqlite3 data/telemetry.db` (таблицы `sessions`, `events`).

## Разработка

```bash
uv venv .venv && uv pip install -p .venv -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
cd server && DB_PATH=/tmp/t.db STATS_TOKEN=x ../.venv/bin/uvicorn app:app --reload --port 8765
```

## Переменные окружения

| Имя | По умолчанию | Смысл |
|---|---|---|
| `DOMAIN` | — | домен для Caddy |
| `ALLOWED_ORIGINS` | `https://sigma.gritsenko.biz,http://localhost:5380` | откуда принимать события |
| `STATS_TOKEN` | пусто (чтение закрыто) | токен для `/stats/*` |
| `RETENTION_DAYS` | `0` (хранить всё) | при старте удалить данные старше N дней |
| `DB_PATH` | `/data/telemetry.db` в контейнере | файл базы |
| `TRUST_PROXY` | `1` в контейнере | брать IP из `X-Forwarded-For` (за Caddy) |
| `ALLOW_NO_ORIGIN` | выкл | принимать запросы без Origin (только для отладки) |

## Приватность

Сервер хранит случайный id клиента, User-Agent, язык браузера и события игры. IP используется только в памяти для ограничения частоты запросов и в базу не пишется. Персональных данных нет; в игре стоит упомянуть сбор статистики и дать выключатель в настройках (`setEnabled(false)`).
