# Direct AI Control в Shtab

Раздел доступен в боковом меню по адресу `/plugins/direct-ai-control`.
Интерфейс подхватывает тему Shtab. API плагина работает в FastAPI ядра и
проверяет сессию Shtab. Вход в рекламный кабинет выполняется на странице плагина.

Администратор устанавливает плагин через `POST /api/plugins` с содержимым
`manifest.json`. Для установки из GitHub укажите URL репозитория в поле `url`
в запросе. Для запуска достаточно обычного стека Shtab.
По умолчанию плагин запускается в деморежиме и создаёт SQLite в
`core/plugins/direct-ai-control/direct-demo.db`. Локальная учётная запись
по умолчанию `admin` / `demo-admin-2026`.

Для HTTPS или подключённого режима задайте `SHTAB_DIRECT_ADMIN_PASSWORD`
до первого запуска. Для существующей базы настройка применяется при создании
учётной записи. `SHTAB_DIRECT_DATABASE_URL` указывает на отдельную базу Direct.
При использовании PostgreSQL примените миграции пакета из workspace Shtab:

```powershell
uv run --all-packages python -m shtab_direct_ai_control.migrate upgrade head
```

При настройке Яндекс OAuth зарегистрируйте HTTPS-адрес
`/api/plugins/direct-ai-control/api/auth/yandex/callback` на домене Shtab.
Настройки Direct передаются с префиксом `SHTAB_DIRECT_`.

Пересборка интерфейса из корня Shtab:

```powershell
npm --prefix direct-ai-control/ui ci
npm --prefix direct-ai-control/ui run build
```

Если плагин уже установлен, обновите собранный `frontend/index.js` и изменённые
файлы Python в его установленной копии, затем перезапустите ядро.
