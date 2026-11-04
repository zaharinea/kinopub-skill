# kinopub-skill

Неофициальный скилл для AI-агентов (Claude Code, Codex, Hermes Agent), который управляет закладками онлайн-кинотеатра [Кинопаб](https://kino.watch) (kino.pub). Пишете агенту «добавь „Дюну“ в закладки», «что у меня в избранном», «перенеси фильм в другую папку» — он ищет фильм и меняет закладки через [API Кинопаба](https://kinoapi.com).

Что умеет:

- показывать папки закладок и фильмы в них;
- искать фильмы и сериалы, показывать карточку и папки, в которых фильм уже лежит;
- добавлять, убирать и переносить фильмы между папками;
- создавать и удалять папки (удаление — только после подтверждения);
- вести список «Буду смотреть».

Нужна действующая подписка Кинопаба. Проект не связан с Кинопабом.

## Требования

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — запускает скрипт и сам ставит зависимости (Python 3.10+, `keyring`).
- Хранилище секретов ОС: macOS Keychain, Secret Service (GNOME Keyring, KWallet) в Linux или Windows Credential Manager.

## Установка

### Claude Code

```bash
claude plugin marketplace add zaharinea/kinopub-skill
claude plugin install kinopub@kinopub-skill
```

В Claude Code 2.1.275 и новее — одной командой внутри сессии:

```
/plugin install kinopub --marketplace zaharinea/kinopub-skill
```

Обновление:

```bash
claude plugin marketplace update kinopub-skill
```

### Codex

```bash
codex plugin marketplace add zaharinea/kinopub-skill
codex plugin add kinopub@kinopub-skill
```

Обновление:

```bash
codex plugin marketplace upgrade kinopub-skill
codex plugin add kinopub@kinopub-skill
```

### Hermes Agent

```bash
hermes skills install zaharinea/kinopub-skill/plugins/kinopub/skills/kinopub
```

### Вручную

Скопируйте папку `plugins/kinopub/skills/kinopub` в каталог скиллов агента, например `~/.claude/skills/kinopub` или `~/.agents/skills/kinopub`.

## Первый запуск

Попросите агента что-нибудь сделать с закладками. Он подключится к Кинопабу как ТВ-устройство:

1. Агент пришлёт короткий код и ссылку https://kino.watch/device.
2. Вы вводите код на сайте (код действует 5 минут).
3. Агент сохраняет токены в хранилище секретов ОС, устройство появляется в списке устройств аккаунта.

После входа агент покажет ваши папки и спросит, какую считать папкой по умолчанию. Ответ он запишет в `~/.config/kinopub/config.md` (шаблон — [config.example.md](plugins/kinopub/skills/kinopub/config.example.md)); файл можно править вручную. Настройки лежат вне папки плагина, поэтому обновление их не стирает.

## Команды CLI

Скилл можно запускать и без агента:

```bash
S=plugins/kinopub/skills/kinopub
$S/run login start            # код для kino.watch/device
$S/run login wait             # ждёт ввода кода
$S/run folders
$S/run show <папка>
$S/run search 'дюна'
$S/run add <фильм> <папка>
$S/run remove <фильм> <папка> # или --all
$S/run move <фильм> <откуда> <куда>
$S/run watchlist
$S/run logout
```

Папка — id, часть названия или ссылка `kino.watch/favorites/view?id=…`; фильм — id или ссылка `kino.watch/item/view/<id>`. Полный список — `$S/run --help` и [SKILL.md](plugins/kinopub/skills/kinopub/SKILL.md).

## Безопасность и приватность

- Пароль от Кинопаба скилл не видит: вход идёт по одноразовому коду на сайте.
- Access- и refresh-токены хранятся только в хранилище секретов ОС (сервис `kinopub-skill`) и никуда, кроме API Кинопаба, не отправляются. `logout` удаляет их; само устройство отвязывается на сайте в списке устройств.
- Для входа используется публичный `client_id` Kodi-плагина [quarckster/kodi.kino.pub](https://github.com/quarckster/kodi.kino.pub), на который ссылается сам Кинопаб.

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `KINOPUB_API` | хост API, по умолчанию `https://api.srvkp.com` (запасной — `https://api.service-kp.com`) |
| `KINOPUB_KEYRING_SERVICE` | имя сервиса в хранилище секретов, по умолчанию `kinopub-skill` |
| `KINOPUB_STATE_DIR` | где ждёт код устройства, по умолчанию `~/.cache/kinopub` |

## Разработка

```bash
uv run --with 'keyring>=24,<26' python -m unittest discover -s tests
```

Тесты не ходят в сеть и не трогают хранилище секретов.

## Лицензия

[MIT](LICENSE)
