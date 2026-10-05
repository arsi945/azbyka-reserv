# Контрольный прогон итоговой версии обходчика (коммит 59723f9) на живом azbyka.ru

Дата: 2026-10-05, 04:00–04:55 UTC. Темп ≤ 1,5 запроса/с (настройки по умолчанию, `min_delay 0.7`),
не логинились, /forum и /znakomstva не открывались (отсечены exclude ещё на уровне карт).

## 0. Итог в двух словах

**Работает:** Библия (перевод-варианты, свёртка стихов в главы, без `~`/`&ru`), тексты богослужений
по шаблону дат, аудио (302 → media.azbyka.ru → файл, имена с `:` и кириллицей), mp3 Библии,
Отечник (оглавление → главы `./N`, ссылки на скачивание помечены `login` и не запрашиваются),
календарь от сегодняшней даты наружу, офлайн-сервер (стих → глава, Range 206, переписывание
ссылок на `/__ext__/media.azbyka.ru/…`), анти-бот/429/403 — ни одного.

**Сломано / требует правки (по приоритету):**
1. **[высокий] Картинки из `<image:loc>` карт сайта всё ещё идут раньше страниц.** За 25 минут
   основного прогона: 1 642 картинки (668 МБ) и только 23 HTML-страницы (из них ни одной главы Библии,
   ни одного богослужения, ни одного дня календаря). Причина — сортировка `ORDER BY priority, depth, id`:
   картинки из карты получают depth 0 (asset не увеличивает глубину, а ветка карт вся на depth 0),
   страницы из карт — depth 1. Приоритет теперь общий (10 у /molitvoslov/), и depth решает в пользу картинок.
2. **[высокий] Белорусский перевод `&v` теряется:** `drop_params` содержит `"v"` (по моей прошлой
   рекомендации — моя ошибка), поэтому `?Gen.1&v` нормализуется в `?Gen.1` → `?Gen.1&r`.
   В базе 52 кода по 1 361 главе, `v` — 0.
3. **[средний] `&ru-p` и `&ru-d` (Победоносцев, Десницкий) отсекаются robots:** правило
   `Disallow: /biblia/?*&ru` без `$` по стандарту совпадает и с `&ru-p`/`&ru-d`. 2 × 1 361 вариантов
   в `skipped_stats` (`robots`, 3 052). Если эти переводы нужны — исключение в коде/конфиге.
4. **[средний] Объём Библии в media-очереди:** к каждой главе каждого перевода прилагается mp3
   (`https://media.azbyka.ru/audio/biblia/utfcs/Lk/5.mp3`, 6–12 МБ). В адресном прогоне media-потоки
   за 12 минут скачали 115 таких файлов (534 МБ) — только Лк в двух переводах. Аудио Библии для всех
   переводов, где оно есть, может составить сотни ГБ и занять media-потоки надолго.
5. **[низкий] Мусорные адреса Библии:** `/biblia/?a`, `/biblia/?c`, … (61 шт., меню выбора перевода
   на `/biblia/`) и `/biblia/?Ps.1&el-r` (неизвестный код из ссылки на `/molitvoslov/`). Безвредно,
   но это 62 лишние страницы с приоритетом 10.
6. **[низкий] Видео не проверено вживую:** API-страницы PeerTube имеют приоритет 58 и за время прогонов
   до них очередь не дошла (см. f).
7. **[низкий] Нестабильный тест:** `tests/test_crawl.py::test_network_outage_does_not_burn_tries`
   один раз упал, затем два раза прошёл (60/60).

## 1. Тесты

`python3 -m pytest -q tests/` (pytest пришлось поставить): первый запуск **1 failed, 59 passed**
(`test_network_outage_does_not_burn_tries`, в логе `HTTP 503 на …/flaky — сайт просит подождать 0 с`),
повторный полный запуск — **60 passed in 41.79s** (`pytest.txt`), отдельный запуск упавшего — passed.
Похоже на гонку по времени в тесте.

## 2. Прогоны

| Прогон | Конфиг | Время | Скачано | Файлы |
|---|---|---|---|---|
| Основной | `fc.toml` (peertube 240p, файлы ≤ 300 МБ), остальное по умолчанию | 25 мин | 2 060 (776,7 МБ), 1 ошибка, 1×404, в очереди 336 915 | `status.txt`, `log-tail300.log`, `console.log` |
| Адресный | `fc2.toml`: 7 стартовых страниц, карты исключены, worships на 2 дня, без seed_days | 12 мин | 1 009 (679,5 МБ), 7×404, 3 редиректа | `status-targeted.txt`, `log-targeted-tail300.log`, `console-targeted.log` |
| Без Библии | `fc3.toml`: как адресный + exclude `/biblia/` и аудио Библии, день 2026-10-05 | 8 мин | 664 (74 МБ), 8×404, 13 редиректов | `status-nobible.txt`, `log-nobible-tail200.log`, `console-nobible.log` |

Адресные прогоны понадобились, потому что основной до страниц не дошёл (п. 0.1).

## 3. Проверки по пунктам

### a. Порядок и что скачано
Основной прогон, по `fetched_at`: robots.txt → 390 карт (приоритет 1) → PDF `molitvoslov/…/slav-eng.pdf`
(media, 10) → `biblia/sitemap/*` (6 «страниц» с приоритетом 1 — у них нет `.xml`, но разобраны как карты:
120 790 адресов Библии в очереди) → `biblia/downloads/bibliya.{fb2,pdf,doc}`, `nz.pdf`, `tsya.pdf`,
`biblia/`, `molitvoslov/`, `bogosluzhenie/`, `chaso-slov/`, `worships/`, `pravo/`, `psalms/` (корни, 10) →
**дальше только картинки** `https://azbyka.ru/molitvoslov/wp-content/uploads/2022/02/p1atbegpsj14sphmbh2pam9omt6.png`
(9,8 МБ) и т.п. Родители скачанных картинок: `molitvoslov/post-sitemap1.xml` — 904, `post-sitemap2.xml` — 589.
Итог по видам: asset 1 642 (668 МБ), page 23 (82 МБ), media 5 (41 МБ), sitemap 390 (23 МБ).
По разделам: /molitvoslov 1 551 (637,8 МБ), /biblia 65 (95 МБ; это карты и downloads), /quotes 154 (карты), …
Распределение depth при приоритете 10: asset depth 0 — 1 981; page depth 0 — 16, depth 1 — 13 015, depth 2 — 51 153.
Очередь основного прогона по приоритетам: 10 → 64 168 стр.; 11 → 19 052 (основные переводы); 12 → 16 839
(дни календаря); 22 → 71 514; 40 → 54 912 (богослужения 2020–2035); 45 → 50 357 (прочие переводы); 58 → 2 (PeerTube).

### b. Библия
- Варианты переводов: по **1 361** адресу на код для 52 кодов (`a am ar az b bg c chn cs cze de_el de_ml el
  el-na28 en-kjv en-nrsv es fi g h he hr is it it-cei74 j jp k kk l lv m mt n no-b o p pl pt r ro s sb se syc t
  u ua utfcs w y z`). Нет `v` (drop_params, п. 0.2), `ru-p`/`ru-d` (robots, п. 0.3).
- `?Gen.1&c`, `?Gen.1&utfcs`, `?Gen.1&g`, `?Mt.1&g`, `?Lk.5&r` — в очереди. Стихов (`?…:…`) в базе **0**,
  `~` — 0, `&ru` — 0. Свёртка подтверждена и в офлайн-сервере (`/biblia/?Lk.5:8` → та же страница, что `?Lk.5&r`).
- Адресный прогон скачал 699 глав Библии в разных переводах (`?Lk.5&r`, `&c`, `&l`, `&a`, `?Lk.1&c` …).
- Толкования `/biblia/in/?…` — 34 748 в очереди, скачано 0 (не дошли).
- Мусор: `https://azbyka.ru/biblia/?a` (+60 таких, родитель `https://azbyka.ru/biblia/`, ссылки `href="/biblia/?a"`),
  `https://azbyka.ru/biblia/?Ps.1&el-r` (родитель `/molitvoslov/`).

### c. Богослужения
Основной прогон: 58 440 адресов `/worships/?date=…&worship=…` (2020-01-01…2035-12-31) в очереди, скачано 0.
Адресный прогон (шаблоны с приоритетом 5) скачал:
| URL | размер | файл |
|---|---|---|
| `https://azbyka.ru/worships/?date=2026-10-05&worship=liturgy` | 251 421 | `azbyka.ru/worships/index@f97fa182.html` |
| `https://azbyka.ru/worships/?date=2026-10-06&worship=liturgy` | 253 372 | `azbyka.ru/worships/index@acd7319e.html` |
| `https://azbyka.ru/worships/?date=2026-10-05&worship=vespers` | 154 202 | `azbyka.ru/worships/index@f72dc27a.html` |

Содержимое (после удаления тегов): литургия — 13 строк «Тропарь», 14 «Кондак», 6 «Прокимен», 3 «Аллилуиа»,
«Отче наш», «Тропарь небесных чинов Бесплотных, глас 4», «Входа нет. Прокимен дня.»; против страницы
без `worship` добавилось **581 строк / 62 тыс. символов** — это сам текст. Вечерня почти совпадает со страницей
дня без параметра (+2 строки): сайт по умолчанию и показывает вечерню (изменяемые части — стихиры на
«Господи, воззвах», тропари). Неизменяемых частей («Свете тихий», «Блажен муж») на сайте нет вообще — не баг.

### d. Календарь
16 802 адреса `/days/2000-01-01 … /days/2045-12-31`, приоритет 12, depth 1. Первые в порядке выдачи:
`/days/2026-10-05`, `2026-10-06`, `2026-10-04`, `2026-10-07`, `2026-10-03` — **от сегодняшней даты наружу** ✔.
В основном прогоне ни один день не скачан (картинки). В прогоне «без Библии» `/days/2026-10-05` скачан
первым после robots (приоритет 5).

### e. Аудио
- Со страницы дня `/days/2026-10-05` (прогон «без Библии»): 13 ссылок вида
  `https://azbyka.ru/audio/audio1/zachala/Phil/Phil.1:1-7.mp3` → **302** `https://media.azbyka.ru/audio/zachala/Phil/Phil.1:1-7.mp3`
  → done, 1 310 443 байт, файл `media.azbyka.ru/audio/zachala/Phil/Phil.1_1-7.mp3` ✔. Так же тропари
  `…/tropari-i-kondaki-sivakov/17_ponedeljnik_…_troparj_glas_4.mp3`, канон `…/ierej-aleksej-zabelin-kanon-angelu-hranitelju.mp3`.
- Аудио из /days/: `https://azbyka.ru/days/storage/media/audio_services/685192f082dea_5%20октября%20Прор.%20Ионы.mp3`
  → 200, 6,7 МБ, файл `azbyka.ru/days/storage/media/audio_services/685192f082dea_5 октября Прор. Ионы.mp3` ✔.
- `/Lk/` в базе: 96 адресов (адресный прогон), напр. `https://media.azbyka.ru/audio/biblia/r/Lk/5.mp3` done (6,1 МБ),
  `https://media.azbyka.ru/audio/biblia/utfcs/Lk/1.mp3` done (11,6 МБ); `…/zachala/Lk/Lk.3:19-22.mp3` done.
- Страница альбома `/audio/nikolaj-serbskij-besedy-na-evangeliya.html` (приоритет 24) в обоих адресных
  прогонах осталась в очереди — плейлист wp-playlist и m4b/m3u вживую **не проверены**.
- Основной прогон: из mp3 скачаны только `wp-content/themes/azbyka/assets/sound/bling{1,2}.mp3` (звуки темы).

### f. Видео (PeerTube)
В очереди: `https://tube.azbyka.ru/api/v1/videos?start=0&count=50&sort=publishedAt&isLocal=true`,
`https://tube.azbyka.ru/api/v1/videos/82775f33-84a3-43c0-9435-f3da6454ff05`, `…/d7af96f0-97cc-4c99-b31d-6d6587bee567`
(kind page, приоритет 58). Превью `https://tube.azbyka.ru/lazy-static/previews/<uuid>.jpg` скачаны (asset).
**API-ответы, задачи на mp4 и скачивание ролика не проверены** — очередь до приоритета 58 не дошла
ни в одном прогоне. Нужен отдельный короткий прогон со стартом с `api/v1/videos/<uuid>` или поднятым приоритетом.

### g. Отечник
Прогон «без Библии»: `/otechnik/Ignatij_Brjanchaninov/tom2-asketicheskie-opyty/` скачан (80 257 байт),
главы `/1`, `/2`, … `/21`, `/11_1` поставлены в очередь (274 адреса /otechnik/ в очереди). Глав скачать не
успели (приоритет 14 < 5/10 у других). Адресов `/otechnik/books/…` в базе **0**; в `skipped_stats`:
`login | otechnik | 5 | https://azbyka.ru/otechnik/books/download/8979/Аскетические-опыты-2-часть.epub` ✔.

### h. Ошибки, паузы, пропуски
- Основной: `error` 1 — `https://azbyka.ru/kliros/post-sitemap1.xml` HTTP 500 (карта сломана на стороне сайта);
  `notfound` 1 — `https://azbyka.ru/bogosluzhenie/wp-content/plugins/wpglobus/flags/cu.RU.png`.
- Адресные: `notfound` — `HTTP 405` на `/worships/wp-content/themes/azbyka-worships/assets/images/favicons/android-icon-{36,48,72,96,192}x…png`
  (теперь корректно считаются «нет»), 404 `…/fonts/PonomarUnicoder.woff`, `/biblia/assets/misc/back.png`, `/assets/fontawesome/style.css`.
- `auth` — 0. Пауз, 403, 429, анти-бот заглушек — **ни одной** (в журналах нет строк «пауза»/«429»/«403»).
  «отложено 1–4» в консоли — повтор после backoff.
- `skipped_stats`, основной: `robots/biblia 3052` (`?Gen.1&ru-p`), `exclude/recept 97` (уменьшенные `-300x200`),
  `exclude/wp-content 42`, `exclude/biblia 40` (`/biblia/search`), `exclude/deti 27`, `exclude/worships 17`
  (`/worships/__chants/`), `exclude/znakomstva 14`, `exclude/forum 13`, `exclude/auth 12`, `exclude/stat.azbyka.ru`
  (`counters`, `rate`, `item-score`, `batch-query`, `batch-rating` — по 6–7), `exclude/sear 6`, `exclude/bibrefs 2`.
  Прогон «без Библии» ещё: `robots/worships 3` — `https://azbyka.ru/worships/?action=download&date=2026-10-05`.

### i. Имена файлов
Все 2 060 / 1 009 / 664 файлов из колонки `path` существуют. Примеры:
- `https://media.azbyka.ru/audio/zachala/Hebr/Hebr.4:14-5:6.mp3` → `media.azbyka.ru/audio/zachala/Hebr/Hebr.4_14-5_6.mp3` (`:` → `_`) ✔
- `…/audio_services/685193031dc74_5%20%D0%BE%D0%BA…mp3` → `azbyka.ru/days/storage/media/audio_services/685193031dc74_5 октября Духовный совет дня.mp3` ✔
- `https://azbyka.ru/biblia/?Lk.5&r` → `azbyka.ru/biblia/index@fddfc09b.html`; `?Lk.5&c` → `index@541d6993.html` ✔
- `https://azbyka.ru/bogosluzhebnye-ukazaniya?date=2026-10-05` → `azbyka.ru/bogosluzhebnye-ukazaniya/index@e4ad9483.html` ✔

### j. Прочие наблюдения
- Помимо шаблонов скачалась `https://azbyka.ru/worships/?worship=chants` (без даты) — дубль «сегодня».
- `/biblia/sitemap/*` классифицируются как `page` с приоритетом 1 (нет `.xml` в имени), но разбираются как карты — ок.
- `biblia/downloads/bibliya.pdf/.doc/nz.pdf/tsya.pdf` в основном прогоне ушли как `page` (ссылки из карты
  страниц, без `kind=media`) — скачались целиком в память (до 30 МБ), сохранены нормально.
- Аудио Библии пойдёт для **всех** переводов, у которых есть звук (`/audio/biblia/<код>/…`): стоит решить,
  нужны ли все, или ограничить кодами `r`, `utfcs`, `c`.

## 4. Офлайн-сервер (`serve`)

`serve --data /tmp/azr-final --port 8099` (основной прогон) и `--data /tmp/azr-final2 --port 8098` (адресный):

| Запрос | :8099 (основной) | :8098 (адресный) |
|---|---|---|
| `/` | 404, 808 Б «Нет в архиве» (главная не скачана — в очереди) | 404, 808 Б (тоже не скачана) |
| `/biblia/?Mt.1&r` | 404 (не скачана) | **200**, 108 381 Б, text/html |
| `/biblia/?Lk.5:8` (стих) | 404 | **200**, 114 652 Б — та же страница, что `/biblia/?Lk.5&r` (114 652 Б) ✔ |
| `/worships/?date=2026-10-05&worship=liturgy` | 404 | **200**, 248 222 Б |
| mp3 с `Range: bytes=0-1023`: `/__ext__/media.azbyka.ru/audio/biblia/utfcs/Lk/5.mp3` | — | **206**, 1 024 Б, `audio/mpeg`, `Content-Range: bytes 0-1023/6674007` ✔ |
| тот же mp3 без Range (`…/r/Lk/5.mp3`) | — | 200, 6 141 903 Б, `Accept-Ranges: bytes` |
| `/__azr__/` | 200, 2 194 Б (меню разделов, статистика, «Поиск ещё не построен») | 200, 2 196 Б |

В HTML главы ссылка на звук переписана: `href="/__ext__/media.azbyka.ru/audio/biblia/r/Lk/5.mp3"` ✔.
Голый путь `/media.azbyka.ru/…` → 404 (ожидаемо, внешние хосты только через `/__ext__/`). Серверы остановлены.
