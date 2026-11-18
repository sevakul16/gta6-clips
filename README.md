# GTA 6 автонарезки

Конвейер работает по кругу на GitHub Actions (workflow `clips`, каждый запуск в конце стартует следующий):
1. слушает чаты живых стримов GTA 6 и ловит всплески активности, плюс берёт популярные клипы зрителей (Twitch API);
2. вырезает момент и прогоняет бесплатные фильтры: есть ли движение в кадре, не сидит ли стример на весь экран;
3. отправляет кадры и расшифровку речи редактору (ветка `review`), тот ставит оценку 1–10, обрезку и подпись (`state/verdicts.json`);
4. раз в `min_gap_minutes` публикует самый сильный из одобренных моментов: вертикаль 1080×1920, сверху вебка, снизу геймплей, субтитры (faster-whisper), TikTok и YouTube Shorts через upload-post.com.

Состояние (что опубликовано, отклонено, ждёт редактора или слота) хранится в `state/posted.json`.

## Управление (`config.yaml`)
- `publish: false` — собирать и оценивать моменты, но ничего не публиковать.
- `autorun: false` — остановить конвейер после текущего запуска.
- `judge.mode` — `review` (редактор читает ветку `review`) или `api` (Claude API, секрет `ANTHROPIC_API_KEY`).
- `cam_overrides` — ручная разметка вебки стримера (доли кадра x, y, ширина, высота).

## Секреты (Settings → Secrets and variables → Actions)
- `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` — приложение на dev.twitch.tv/console
- `UPLOAD_POST_KEY` — API key из upload-post.com (профиль `gta6` с подключёнными TikTok и YouTube)

## Ручной запуск
Actions → clips → Run workflow. `dry_run` — всё, кроме публикации; `chain` — запустить цикл.
Кадры кандидатов последнего запуска лежат в ветке `previews`, готовые ролики — в Artifacts запуска.
