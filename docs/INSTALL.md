# Установка tgpanel

Поддерживаются Ubuntu 24.04 и Debian 13 (x86_64), запуск от root. Перед установкой на сервере
должен работать WEB proxy ([tproxy-server](https://github.com/telegramdesktop/tproxy-server#readme)):
установщик это проверяет первым и, если нет, выходит с кодом 1 ничего не изменив.

## Установка

```bash
curl -fsSL https://raw.githubusercontent.com/barakov-dot/tg-proxy-2/main/install.sh -o install.sh
printf '%s\n' '<токен бота>' > /root/bot.token && chmod 600 /root/bot.token
sudo bash install.sh --panel-domain panel.example.com --bot-token-file /root/bot.token --admin-id <telegram id>
```

Токен и пароль можно передать и переменными окружения `TGPANEL_INSTALL_BOT_TOKEN`,
`TGPANEL_INSTALL_PANEL_PASSWORD` (в `ps` они не видны, в отличие от `--bot-token`).

Без терминала (cron, автоматизация) обязательны `--yes` и явное `--import` или `--no-import`.
`--yes` не пропускает проверку DNS: для этого есть отдельный `--ignore-dns`.

## Выбор версии (`--ref`)

- Без `--ref` ставится последний релизный тег вида `vX.Y.Z`. Если тегов ещё нет, ставится
  движущаяся ветка `main`; установщик предупреждает об этом.
- Для воспроизводимой установки зафиксируйте версию: `--ref v0.1.0` (тег) или `--ref <sha коммита>`.
- Выбранный режим запоминается: `tgpanel update` без аргументов следует ему (последний релиз или
  выбранный `--ref`). Сменить версию: `tgpanel update --ref <тег>`.

## Повторный запуск и обновление

Повторный запуск `install.sh` — это обновление: путь панели, логин, пароль и секреты не
меняются. Код обновляется тем же путём, что и `tgpanel update` (резервная копия, миграции,
откат при неудачном запуске). Если указать другой `--panel-domain` или токен бота, установщик
спросит подтверждение и применит изменение целиком: файл настроек, базу и блок Caddy.

Пароль, показанный при установке, дублируется в файле `/etc/tgpanel/.first-run-credentials`
(0600) до конца установки; в итоговом сообщении он показывается в последний раз, файл удаляется.

## Команды

`tgpanel doctor`, `repair`, `update`, `uninstall [--purge] [--force]`, `show-url`, `reset-password`.

`uninstall` восстанавливает профили из копии `pre-install`; без копии он отказывается работать
без `--force`. После удаления копия переименовывается в `*-pre-install-used`, чтобы новая
установка сняла свежий снимок. `/var/lib/caddy` не затрагивается никогда.

Проверки на сервере: `scripts/server-selfcheck.sh`.
