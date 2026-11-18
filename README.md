# GTA 6 автонарезки

Каждые 30 минут GitHub Actions:
1. берёт самые просматриваемые клипы GTA 6 за сутки у стримеров из `config.yaml` (Twitch API);
2. находит вебку стримера (детектор лиц) и собирает вертикаль 1080×1920: сверху реакция, снизу геймплей;
3. распознаёт речь (faster-whisper) и вшивает крупные субтитры;
4. публикует в TikTok и YouTube Shorts через upload-post.com с подписью и ссылкой на стримера.

Не больше `max_posts_per_day` роликов в сутки на профиль. Опубликованные клипы пишутся в `state/posted.json`, чтобы не было повторов.

## Секреты (Settings → Secrets and variables → Actions)
- `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` — приложение на dev.twitch.tv/console
- `UPLOAD_POST_KEY` — API key из upload-post.com (профиль `gta6` с подключёнными TikTok и YouTube)

## Ручной запуск
Actions → clips → Run workflow. Галочка `dry_run` — только собрать ролики без публикации (их можно скачать во вкладке запуска, раздел Artifacts).

## Если вебка вырезается не та
Добавь стримеру ручную разметку в `cam_overrides` в `config.yaml` (доли кадра x, y, ширина, высота).
