# Разведка живого сайта azbyka.ru — факты для настройки обходчика

Дата проверки: 2026-10-04/05 (UTC). Окружение: облачный контейнер, выход в сеть через прокси.
User-Agent: `Mozilla/5.0 (compatible; azbyka-reserv/0.1; offline-archive)`. Не логинились,
форм не отправляли, /forum и /znakomstva не трогали. Всего запросов разведки ≈ см. раздел «Бюджет».

Сырые материалы в этой папке:

- `20261004-211101/` — отчёт встроенного `probe --limit 4` (report.md, report.json.gz, samples/);
  `probe-run.log` — его консольный вывод.
- `live-samples/` — HTML-образцы страниц, снятые вручную curl'ом (имя = URL).
- `sitemaps/` — сводка по всем картам сайта (`sitemaps-summary.json`, `sitemap-counts.tsv`,
  `all-sitemap-urls.txt.gz` — все URL из всех карт).
- `crawl-test/` — результаты 20-минутного пробного обхода (status, хвост журнала, выборки из sqlite).

---

## 0. Главное (TL;DR)

1. **Доступ есть.** `https://azbyka.ru/` → `HTTP/2 200`, `server: ddos-guard`. Челленджа DDoS-Guard
   (JS/капча) для честного UA и curl/urllib **не было** ни разу; ставятся куки `__ddg1_`, `__ddg8_`,
   `__ddg9_`, `__ddg10_` (ни на что не влияют). 429/503 за всю разведку не получили.
2. **robots.txt запрещает почти всё медиа**, которое мы хотим сохранить:
   `Disallow: /*.mp3$`, `/*.epub$`, `/*.djvu$`, `/*.txt$`, `/*.mobi$`, `/audio/audio1/audiobooks/*.m4b`,
   `/*/wp-content/uploads/bg_forreaders/*` (PDF/EPUB/FB2 «для читалок»), `/otechnik/books/download/*`,
   `/otechnik/books/original/*`, `/worships/?date=*&`, `/vopros/question/*`.
   А хосты **`media.azbyka.ru`** (все mp3 аудио и Библии) и **`tube.azbyka.ru`** (всё видео) отдают
   `robots.txt` = `User-agent: * / Disallow: /`.
   ⇒ При `respect_robots = true` (по умолчанию) обходчик **не скачает ни одного mp3, m4b, EPUB, PDF
   «для читалок» и ни одного видео**. Нужно решение владельца проекта: либо `respect_robots=false`
   с ручным набором exclude (рекомендуется — список ниже), либо точечные `allow`-исключения в коде.
3. **Clean-param из robots.txt вредит**: обходчик вырезает параметры из Clean-param, а там есть
   `worship` для `/worships/*` (без него не получить тексты служб) и глобальные `file`, `url`,
   `mode`, `time`, `option`, `preview`, `off`, `save`. См. §3.f.
4. **Скачивание книг /otechnik/ (EPUB/PDF/сканы) — только после входа**: аноним получает
   `302 → https://azbyka.ru/auth/?reflink=…download-return?target=<base64>`. Обходчик это корректно
   помечает статусом `auth` (по слову `auth` в Location). Без cookies_file файлов Отечника не будет.
5. **Видео — собственный PeerTube `tube.azbyka.ru`** (6 267 роликов, ≈ 2 978 часов), YouTube почти
   не используется. Файлы скачиваются по прямым ссылкам из API PeerTube (`/api/v1/videos/{uuid}`),
   в HTML страниц /video/ их нет (там только `data-id="<uuid>"`). Нужен отдельный загрузчик или
   шаблон; yt-dlp тоже умеет PeerTube.
6. Ловушки: бесконечные календари `/days/ГГГГ-ММ-ДД` (валидны минимум 1990…2100), **`/biblia/days/ГГГГ-ММ-ДД`**
   (не покрыт `date_filter_patterns`!), `/worships/?date=…`, `?print=1`, `/obratnaja-svjaz?the_url=…`
   на каждой странице, `/page/N/?content_only=1`, многоколоночная Библия `?Mt.1&r~c~…`,
   `/otechnik/ajax/book/load-chapter/{id}/{n}`, телеметрия `stat.azbyka.ru`, `ajax.azbyka.ru`.
7. `azbyka.org` — **не зеркало**: Cloudflare managed challenge (`HTTP/2 403`, `cf-mitigated: challenge`,
   «Just a moment…»). `alias_fallback` для него бесполезен (получим 403-страницу).

---
## 1. Доступ, анти-бот, протокол (п. 3.i)

- `curl -sSI https://azbyka.ru/` → `HTTP/2 200`, `server: ddos-guard`, `cache-control: public, max-age=300`,
  `x-cache-status: HIT`. HTTP→HTTPS: `http://azbyka.ru/…` → `301 https://azbyka.ru/…`;
  `www.azbyka.ru` → `301 https://azbyka.ru/`.
- DDoS-Guard: только куки `__ddg1_ __ddg8_ __ddg9_ __ddg10_`, **никаких JS-челленджей/капчи** для
  curl и для urllib обходчика (за ≈ 2 000+ запросов). 429/503 не было ни разу при темпе ≈ 1 запрос/с.
- gzip работает (`content-encoding: gzip`, `vary: Accept-Encoding`); HTML страницы /otechnik/ 50 КБ
  сжатыми ↔ ≈ 680 КБ без сжатия. HTTP/2 и HTTP/1.1 оба отвечают 200. Время ответа 0,4–1,5 с
  (иногда до 3–8 с у тяжёлых WP-страниц и у `palomnik/api.php`).
- Один сбой urllib: `https://azbyka.ru/worships/feed/` → `IncompleteRead(196337 bytes read)`
  (обрыв chunked-ответа на большом RSS). Обходчик воспринимает как сетевую ошибку и повторит — норм.
- Сабдомены:
  | хост | что | сервер | robots.txt |
  |---|---|---|---|
  | `media.azbyka.ru` | все mp3 (аудио-раздел, Библия) | nginx/1.24.0, без DDoS-Guard | `Disallow: /` |
  | `tube.azbyka.ru` | PeerTube, всё видео | — | `Disallow: /` |
  | `stat.azbyka.ru` | счётчики/рейтинги (`/rate`, `/counters`, `/batch-query`, wss) | — | **исключить** |
  | `ajax.azbyka.ru` | `/bibrefs` — всплывающие цитаты Писания (AJAX) | — | **исключить** |
  `media.azbyka.ru/` → `302 https://azbyka.ru/audio`; `media.azbyka.ru/audio/` → 403 (листинга нет).
- Сторонние хосты, нужные для отображения: `fonts.googleapis.com`, `fonts.gstatic.com`,
  `cdn.jsdelivr.net` (в т.ч. `npm/yandex-metrica-watch/tag.js` — счётчик, блок), `unpkg.com`
  (`@peertube/embed-api`). Счётчики: `mc.yandex.ru`, `cackle.me` (комментарии) — блок.
- `azbyka.org`, `www.azbyka.org` → **Cloudflare managed challenge** (`HTTP/2 403`,
  `cf-mitigated: challenge`, страница «Just a moment…», «Enable JavaScript and cookies»). Это не
  зеркало для нас: оставить алиас на azbyka.ru, но **не делать fallback** на azbyka.org.

## 2. robots.txt (п. 3.a)

Полный текст: `20261004-211101/samples/azbyka.ru_robots.txt.txt` (18 991 байт). `Crawl-delay` нет.
Для `User-agent: Claude-User / Claude-SearchBot / OAI-SearchBot / ChatGPT-User` — `Allow: /`
(к нашему UA не относится; обходчик выбирает группу `*`).

Существенное для архива (проверено функцией `Robots.allowed()` самого обходчика на живом файле):

| URL | allowed() |
|---|---|
| `https://azbyka.ru/audio/audio1/…/x.mp3` | **False** (`/*.mp3$`) |
| `https://azbyka.ru/audio/audio1/audiobooks/parts/5370_part_0.m4b?v=1` | **False** |
| `https://azbyka.ru/otechnik/books/download/…epub` | **False** |
| `https://azbyka.ru/molitvoslov/wp-content/uploads/bg_forreaders/…pdf` | **False** |
| `https://azbyka.ru/days/storage/media/audio_services/….mp3` | **False** |
| `https://azbyka.ru/worships/?date=2026-10-05&worship=liturgy` | **False** (`/worships/?date=*&`) |
| `https://azbyka.ru/biblia/?Mt.1&ru` | False (`/biblia/?*&ru` — версия «с ударениями») |
| `https://azbyka.ru/biblia/?Mt.1&r` | True |
| `https://azbyka.ru/kliros/wp-content/uploads/…pdf` | True |
| `https://azbyka.ru/wp-content/uploads/…jpg`, `/wp-includes/js/…js` | True |
| `https://azbyka.ru/days/2026-10-05`, `/quotes/page/2/` | True |

Прочие заметные Disallow: `/*.txt$`, `/*.djvu$`, `/*.mobi$`, `/wp-` (корневой wp-*, но `Allow: */uploads`,
`*.js`, `*.css`, `*.jpg|png|svg|gif|ico|woff|ttf|otf` — **`.webp` в корневом `/wp-content/` запрещён**),
`*?s=`, `*/feed`, `/*/comment-page-*`, `*content_only=1*`, `/worships/calendar/`, `/days/api*`,
`/days/search`, `/biblia/search*`, `/otechnik/search*`, `/otechnik/book/annotation/*`, `/quotes/*?`,
`/vopros/question/*`, `/test/*?question*`, `/palomnik/Служебная:*`, `/palomnik/Файл:*`,
`/palomnik/index.php?title*`, `/palomnik/Участник:*`, `/sear*`, `/books/*`, `/old/*`.

**Clean-param** (обходчик их применяет при `respect_robots=true`):
- глобально (без префикса): `SuperSocializerEmail par bookmark nav etext _debug amp mode module file off
  add_sql save confirm_key dmid main_event tpclid disableGlobalInfoCollect spush url playlist nowprocket
  bx_sender_conversion_id utm_referer clckid mbstx time sfnsn preview brid wpmp_switcher option ttclid
  loginerror _wpnonce request_form_location wppb_referer_url`;
- `/worships/*`: `church worship patronal top_level`; `/days/*`: `o z h lv fi`; `/quotes/`: `q`;
  `/obratnaja-svjaz`: `the_url`; `/test/*`: `act`; `/biblia/search*`: `in`.
- ⚠ Из-за `worship` все `/worships/?date=…&worship=…` схлопнутся в `/worships/?date=…` (а это только
  «шапка» дня без текстов служб). Глобальные `file`, `url`, `mode`, `time`, `option`, `preview` могут
  ломать и другие адреса. Рекомендация: **не применять Clean-param автоматически** (или только с
  явным списком параметров-мусора, которые и так есть в `drop_params`).

Объявленные в robots карты (51 шт., все отвечают 200 XML; `https://azbyka.ru/sitemap.xml` → 301):
см. `sitemaps/sitemap-counts.tsv`. Обходчик берёт их из robots сам — дополнительные не нужны.

## 3. Карты сайта: точное число страниц по разделам (п. 3.a, 3.k)

Обойдено 416 карт (49 индексов + 367 конечных; из 146 карт `/quotes/post-sitemapN.xml` скачано 54,
остальные пропущены для экономии бюджета — по 200 URL в каждой, итог цитат взят из WP REST API).
Уникальных URL во всех картах: **218 752** (+ ≈ 18 400 непрочитанных цитат). Списки URL:
`sitemaps/all-sitemap-urls.txt.gz`; по каждой карте — `sitemaps/sitemap-counts.tsv`.

| Карта (корень) | URL | дочерних карт | `<image:loc>` | Примечание |
|---|---:|---:|---:|---|
| /biblia/sitemap | 51 304 | 6 | 0 | 1 361 глава `?Gen.1&r`; толкования по стихам `/biblia/in/?Mt.1:1` 26 793 (ВЗ) + 7 955 (НЗ); подстрочник+Стронг 14 880; `/biblia/days/ДАТА` 304 (только 2026 г.); 11 страниц |
| /forum/sitemap.xml | 48 768 | 1 | 3 559 | исключено |
| /quotes/ | 30 074* | 153 | 0 | *с учётом пропущенных карт; WP API: `X-WP-Total: 29109` записей |
| /otechnik/sitemap.xml | 29 828 | 6 | 0 | **только корни книг**, главы не перечислены (см. §4) |
| /palomnik/Sitemap.xml | 16 683 | 2 | 0 | ns0 статьи 15 434 + категории 1 249 |
| /recept/ | 9 597 | 11 | 9 544 | WP API 8 728 записей |
| /vopros/ | 9 570 | 13 | 29 | `question-sitemapN.xml` → `/vopros/<slug>/` |
| /days/sitemaps.xml | 7 671 | 2 | 0 | святые `/days/sv-*` 6 035; иконы/праздники/месяцеслов 1 636 |
| /news/ | 4 724 | 7 | 0 | |
| /sitemap_index.xml (корень) | 3 517 | 6 | 3 494 | энциклопедия: ≈ 3 400 статей `/slug` в корне + `/1/*`, `/dictionary/NN` |
| /fiction/ | 3 123 | 9 | 2 524 | WP API 3 097 |
| /kliros/ | 2 735 | 7 | 0 | WP API 2 549 |
| /molitvoslov/ | 2 013 | 5 | 2 035 | |
| /deti/ | 1 953 | 4 | 3 674 | |
| /video/ | 1 554 | 4 | 38 | WP API 1 481 (страницы-плейлисты PeerTube) |
| /art/ | 1 505 | 11 | 1 264 | |
| /zdorovie/ | 1 492 | 5 | 2 023 | |
| /audio/ | 1 393 | 4 | 41 | WP API 1 165 |
| /znakomstva/ | 1 262 | 9 | 596 | исключено |
| /garden/ | 959 | 3 | 2 405 | |
| /pravo/ | 827 | 4 | 1 | |
| /sueverie/ | 801 | 3 | 509 | |
| /life/ | 718 | 6 | 3 055 | |
| /way/ | 671 | 6 | 137 | |
| /semya/ | 619 | 3 | 294 | |
| /test/ | 567 | 5 | 15 | |
| /memo/ | 473 | 4 | 20 | |
| /propovedi/ | 384 | 3 | 5 | |
| /bogosluzhenie/ | 314 | 3 | 194 | |
| /shemy/ | 303 | 3 | 201 | |
| /katehizacija/ | 273 | 3 | 27 | |
| /crosswords/ | 221 | 3 | 0 | |
| /palomnik/blogs/ | 170 | 3 | 259 | |
| /trezvost/ | 163 | 3 | 106 | |
| /apokalipsis/ | 152 | 3 | 4 | |
| /parkhomenko/foto/ | 150 | 2 | 5 154 | |
| /chinaorthodox/ | 138 | 4 | 101 | |
| /parkhomenko/ | 134 | 3 | 1 079 | |
| /worships/ | 128 | 3 | 3 | статичные чины; ежедневные тексты — не в карте (§7) |
| /viktorina/ | 127 | 3 | 0 | |
| /frontinskiy/, /japanorthodox/, /foto/, /death/, /quiz/, /opros/, /chaso-slov/, /psalms/, /fond/, /games/ | 73, 69, 61, 61, 50, 38, 18, 18, 17, 7 | | /foto/ 1 134 | |

WP REST API (`/<раздел>/wp-json/wp/v2/posts?per_page=1` → заголовок `X-WP-Total`; robots запрещает
`/*/wp-json/`, проверено единично): audio 1165, video 1481, fiction 3097, molitvoslov 2014, deti 1859,
art 1241, foto 43, recept 8728, news 4702, kliros 2549, pravo 783, vopros 670 (плюс вопросы — отдельный
тип), quotes 29109, shemy 258, worships 71, parkhomenko 108, test 1, viktorina 114, корень 3482 записи
+ 46 страниц (`/wp-json/wp/v2/pages`). Своих типов записей у корневого WP нет (types: post, page, …).

## 4. /otechnik/ — святоотеческая библиотека (п. 3.b)

Движок — **Symfony** (не WordPress): ассеты `/otechnik/assets/...-<хэш>.js|css`, Stimulus, importmap
с ≈ 60 модулями (включая `assets/controllers/admin/*` — обходчик их тоже скачает, это ок, 1 раз).

**Типы страниц** (образцы в `live-samples/`):
- Корень `/otechnik/` — список рубрик `/otechnik/<rubrika>/` (≈ 30: molitva, bogoslovie, pravila,
  world, serbian, greek, Spravochniki …).
- Рубрика `/otechnik/molitva/` — ссылки на книги `https://azbyka.ru/otechnik/<Avtor>/<kniga>/` и на
  отдельные главы; плюс `https://azbyka.ru/otechnik/book/annotation/<id>` (**Disallow** в robots —
  исключить, это всплывающие аннотации).
- Автор `/otechnik/Antonij_Surozhskij/` — 165 ссылок на книги; якоря `#sorted-…`; фото
  `/otechnik/authors/<md5>.png`; ссылка на аудио `/audio/1/propovedi-i-besedy/<avtor>`.
- Книга (оглавление) `/otechnik/Ignatij_Brjanchaninov/tom2-asketicheskie-opyty/`:
  `<div id="book-chapters" data-book-id="8979" data-book-chapters-count="21">`. Оглавление —
  **относительные ссылки `href="./1"`, `./11`, `./11_1`, `./21_8`** (глава и подглава через `_`).
  Однoглавные книги (≈ 2/3 выборки) содержат весь текст прямо на странице книги.
- Глава `/otechnik/<Avtor>/<kniga>/11`: canonical = сама глава; ссылки только на соседние главы
  (`/10`, `/12`) и на корень книги. Подглавы (`11_1`) есть **только в оглавлении** книги.
- «Читать полностью» — **не отдельная страница**, а AJAX:
  `GET /otechnik/ajax/book/load-chapter/{bookId}/{n}` (JSON с HTML главы) и
  `/otechnik/ajax/book/has-more-chapters/{bookId}/{n}` (из `assets/js/public/load-more-text-btn-*.js`).
  Это дубль глав — **исключить** `^https://azbyka\.ru/otechnik/ajax/`. Варианта «вся книга одной
  страницей» по обычному URL нет; `?print`/`/all` не обнаружены.
- Карты: `other.xml` (1 209: авторы/рубрики), `public-book-simple-partition1/2.xml` (20 042 + 955
  корней книг), `public-book-two-nesting-level-partition.xml` (4 599 подглав вида `/1_2`),
  `summary-and-file-book.xml` (2 987), `id-uri-book.xml` (36 вида `/otechnik/books/25625-slug/`).
  **Обычные главы `/N` в карты не входят** — обходчик найдёт их только по оглавлению.
- Оценка объёма: выборка 15 случайных книг из карты → глав по оглавлению: 67, 225, 0, 0, 0, 55, 0, 0,
  0, 0, 0, 81, 0, 0, 3 (0 = однoглавная). Среднее ≈ 29 страниц/книга ⇒ **≈ 400–700 тыс. HTML-страниц**
  (≈ 21 тыс. книг; страница 65–420 КБ без сжатия, медиана ≈ 80 КБ ⇒ **≈ 40–60 ГБ HTML** без сжатия).
- Цитаты Писания в тексте: `href="https://azbyka.ru/biblia/?Lk.5:8"` (стихи/диапазоны — query
  варианты Библии, см. §5) и `data-title="?title=Lk&chapter=5:8&type=t_verses&lang=ru"` (атрибут,
  обходчик его не трогает — хорошо).
- Комментарии: виджет `cackle.me` (`data-book--cackle-widget-widget-id-value`) — внешний.

**Файлы для скачивания** (EPUB/PDF/сканы) — блок `.book-download-panel`, видны и анониму:
```
https://azbyka.ru/otechnik/books/download/8552/%D0%A3%D1%87%D0%B8%D1%82%D0%B5%D1%81%D1%8C-%D0%BC%D0%BE%D0%BB%D0%B8%D1%82%D1%8C%D1%81%D1%8F.epub   (rel=nofollow, class="epub")
https://azbyka.ru/otechnik/books/download/8552/<Название>.pdf
https://azbyka.ru/otechnik/books/original/23307/<Название>.pdf    (скан-оригинал, есть у ~60% выборки)
```
FB2 на /otechnik/ не встречен. Анонимный GET/HEAD любой такой ссылки:
```
HTTP/2 302
content-type: text/html; charset=utf-8
location: https://azbyka.ru/auth/?reflink=https%3A%2F%2Fazbyka.ru%2Fotechnik%2Fdownload-return%3Ftarget%3DL290ZWNobmlrL2Jvb2tzL2Rvd25sb2FkLzg1NTIv...   (target = base64 пути)
```
Content-Type/Disposition файла анонимно не получить. Обходчик пометит `auth` (Location содержит `auth`)
— корректно. Обе формы ссылок в robots `Disallow`.

## 5. /biblia/ — Библия (п. 3.c)

Собственный PHP-движок (`/biblia/assets/js/*.js?v=4.4`, jQuery).
- **Глава**: `https://azbyka.ru/biblia/?<Книга>.<глава>&<перевод>` — напр. `?Mt.1&r`, `?Gen.50&r`.
  Без кода перевода (`?Mt.1`) отдаёт синодальный, canonical = `?Mt.1&r` (дубль!). Стих/диапазон:
  `?Lk.5:8`, `?Lk.12:16-22`, `?Mt.1:1-5&r` — варианты той же главы (подсветка). Многоколоночный
  режим: `?Mt.1&r~c` → canonical `?Mt.1&r` (в robots закомментирован `/biblia/?*~*~`).
- **Перевод переключается query-флагом** (не куки и не JS): `?Mt.1&c` → canonical `?Mt.1&c` и т.д.
  Коды (из `data-lang-code`, 56 шт.): `r` Синодальный, `c` ЦСЯ (рус. дореф.), `utfcs` ЦСЯ (цс-шрифт),
  `cs` ЦСЯ (гражд.), `ru` Синод. с ударениями (**Disallow** `/biblia/?*&ru`), `b` еп. Кассиан,
  `j` Аверинцев, `ru-p` Победоносцев, `ru-d` Десницкий, `ar`, `n` (араб.), `az`, `am`, `v` (белорус.),
  `bg`, `chn`, `hr`, `cze`, `a` NKJV, `en-kjv`, `en-nrsv`, `es` (эст.), `fi`, `h` (фр.), `y`, `u` (груз.),
  `de_ml`, `m`, `de_el`, `g` Greek NT Byz, `el`, `el-na28`, `he`, `is`, `it-cei74`, `it`, `jp`, `kk`,
  `s` (кирг.), `l` Nova Vulgata, `lv`, `mt`, `no-b`, `pl`, `ro`, `pt`, `sb`, `z` (серб.), `w` (исп.),
  `se`, `syc`, `p` (тадж.), `t` (тат.), `ua`, `k` (Огиенко), `o` (узб.).
- **Перечень глав**: `https://azbyka.ru/biblia/sitemap/chapters` — 1 361 URL `?<Книга>.<N>&r`
  (в XML как `&amp;r`; `extract.sitemap_links` раскодирует). Конечная схема: 1 361 глава × 55 переводов
  (без `ru`) ≈ **74 900 URL** (часть переводов — только НЗ/ВЗ, сервер отдаст пустую колонку). Шаблон:
  `https://azbyka.ru/biblia/?{chapter}&{code}` для каждого `{chapter}` из карты.
- **Толкования**: отдельные страницы на каждый стих `https://azbyka.ru/biblia/in/?Mt.1:1`
  (title «…глава 1 стих 1 - толкования»), 34 748 шт. в картах `sitemap/interp/ot|nt`; на странице
  ссылки на главы /otechnik/ (Феофилакт, Лопухин и пр.) и на соседние стихи (`/biblia/in/?Mt.1:0` …).
  На странице главы — ссылки прямо на главы толкователей в /otechnik/.
- Подстрочник/словарь Стронга: `/biblia/podstrochnik/<Книга>.<N>` (≈ 680), `/biblia/strong/greek/<N>`
  (5 523), `/biblia/strong/hebrew/<N>` (8 674).
- **`/biblia/days/ГГГГ-ММ-ДД`** — чтения дня, ссылки «вчера/завтра» ⇒ бесконечная цепочка. В карте только
  2026 год (304). Не покрыто `date_filter_patterns` — добавить.
- Скачивание целиком: `/biblia/downloads/bibliya.pdf`, `bibliya.fb2`, `bibliya.doc`, `nz.pdf`, `tsya.pdf`.
- Аудио глав: `https://media.azbyka.ru/audio/biblia/r/Mt/1.mp3` (`audio/mpeg`, 3,5 МБ,
  `Content-Disposition: attachment`, `Accept-Ranges: bytes`) — шаблон `/audio/biblia/<код>/<Книга>/<N>.mp3`.
- Служебные: `/biblia/login?return_to=…` (исключено), `/biblia/personal/favorites`, `/biblia/search`.

## 6. /audio/ (п. 3.d)

- WordPress. Страница альбома `/audio/<slug>.html` содержит `<script type="application/json"
  class="wp-playlist-script">` (подтверждено), напр. `nikolaj-serbskij-besedy-na-evangeliya.html` —
  56 треков: `{"src":"https://azbyka.ru/audio/audio1/Svjashhennoe_pisanie/nikola_serbski_tolk/Besedy_na_Evangelie_01.mp3","type":"audio/mpeg","meta":{"length":2357,...}}`.
- **mp3 по ссылкам `https://azbyka.ru/audio/audio1/…mp3` → `302` на
  `https://media.azbyka.ru/audio/<тот же путь без audio1>`** (`Content-Length` 33 МБ, `Accept-Ranges: bytes`,
  `Content-Type: audio/mpeg`, `Content-Disposition: attachment`).
- M4B: `https://azbyka.ru/audio/audio1/audiobooks/parts/5370_part_0.m4b?v=1791091488` — `200`,
  **`Content-Type: application/octet-stream`, `Content-Length: 2 054 885 465` (2 ГБ!)**, `Accept-Ranges: bytes`.
  Это склейка тех же mp3 — дубль. Рекомендую m4b не качать (или в самом конце).
- M3U: `https://azbyka.ru/audio/wp-content/uploads/bg_playlist/5370.m3u` — 12,9 КБ,
  **`Content-Type: application/octet-stream`** ⇒ в обходчике M3U не будет разобран (ctype не в
  `PARSE_CTYPES`; см. §13). Ссылки на mp3 всё равно берутся из JSON-плейлиста страницы.
- `/audio/wp-json/wp/v2/posts` работает: `X-WP-Total: 1165` (но в robots `Disallow: /*/wp-json/`).
- В /days/: `https://azbyka.ru/days/storage/media/audio_services/685192d561072_4 октября Духовный совет дня.mp3`
  (пробелы и кириллица в имени).

## 7. /video/ (п. 3.e)

- Видео **самохостинг на PeerTube `tube.azbyka.ru`**. На странице `/video/<slug>/`:
  `<div id="video_container" style="…url(//tube.azbyka.ru/lazy-static/previews/<uuid>.jpg)">` и
  плейлист `<a class="video-playlist-link" href="#" data-id="daa1b623-c925-42fa-978e-0a9e5efc60e2">`;
  плеер `@peertube/embed-api`. iframe/mp4 в HTML **нет** (на 3 проверенных страницах; youtube
  встречается единично: ссылка на плейлист в /parkhomenko/).
- API PeerTube без авторизации: `GET https://tube.azbyka.ru/api/v1/videos?count=100&start=N` →
  `total: 6267`; суммарная длительность **2 978 ч**; каналы: azbyka_channel 3 157, root_channel 2 890,
  george_maximov_channel 220. `GET /api/v1/videos/<uuid>` → `downloadEnabled: true`, файлы:
  `https://tube.azbyka.ru/download/streaming-playlists/hls/videos/<uuid>-360-fragmented.mp4`
  (и `static/streaming-playlists/hls/<uuid>/<…>-360-fragmented.mp4`, `…-master.m3u8`).
- Размеры (выборка 5 роликов): 1080p 20 МБ/мин; 720p ≈ 10; 480p ≈ 5–7; 360p ≈ 3,5–4; 240p ≈ 2.
  Максимальное качество в среднем ≈ 5–7 МБ/мин ⇒ **≈ 0,9–1,2 ТБ**; минимальное (240/360p) ≈ **0,4–0,5 ТБ**.
- Обходчик видео не возьмёт (ссылки только через JS/API; robots `Disallow: /`). Нужен отдельный этап:
  перечислить `api/v1/videos`, для каждого взять `fileDownloadUrl` нужного разрешения (или yt-dlp,
  он поддерживает PeerTube).

## 8. /days/ — календарь (п. 3.f)

- День: `https://azbyka.ru/days/2026-10-04` (42 КБ сжатого HTML, 215 уникальных ссылок): соседние дни
  `/days/2026-09-28 … /days/2026-11-07`, святые `/days/sv-<slug>` (≈ 19 на день), праздник
  `/days/prazdnik-<slug>`, иконы `/days/storage/images/icons-of-saints/<id>/<file>.jpg`,
  `/days/menology/*`, `/days/p-*` (статьи), `/biblia/days/2026-10-04`, аудио `/days/storage/media/…mp3`,
  десятки ссылок на энциклопедию в корне и на главы /otechnik/.
- Диапазон: 1990-01-01, 1999-12-31, 2000-01-01, 2050-01-01, 2099-12-31, 2100-01-01 → 200;
  1900-01-01 и 3000-01-01 → 404. ⇒ бесконечный календарь, фильтр дат обязателен (текущий 2000–2045 ок).
- Годовой календарь: `/days/calendar` и `/days/calendar/2026` (≈ 511 ссылок на дни года),
  `/days/calendar/2030` → 200, `/days/calendar/2026/10` → 404.
- Конечные множества (из карт): святые `/days/sv-*` 6 035; иконы `/days/ikona-*`, праздники
  `/days/prazdnik-*`, `/days/menology/*`, `/days/p-*`, `/days/nedelja-*` — всего 1 636.
- API: `/days/api-help` (описание виджетов), `/days/widgets/presentations.json` (200, 3 КБ),
  `/days/widgets/saints?css=1` (iframe-виджеты). `/days/api-v2/doc` → `302 /days/login`; полноценное
  API — после регистрации `/days/register/userapi` (не трогали). `/days/api*` в robots Disallow.
- Clean-param `/days/*`: `o z h lv fi`.

## 9. /worships/ — «Богослужение сегодня» (п. 3.f)

- `https://azbyka.ru/worships/` — текущий день; дата задаётся `?date=ГГГГ-ММ-ДД`
  (canonical всегда `https://azbyka.ru/worships/`, в title добавляется «(2026-10-05)»).
- Без параметра `worship` страница — только «шапка» (меню служб, ≈ 37 КБ). Тексты служб:
  `?date=2026-10-05&worship=<код>`, коды из `<select id="bg_worship_type">`:
  `vespers, matins, chas1, chas3, chas6, liturgy, chas9, requests, chants` (ещё встречается
  `data-key="compline"`). Пример `…&worship=liturgy` = 57 КБ с тропарями/кондаками/чтениями.
- Переход по дням и выбор службы — **только JS** (кнопки без href) ⇒ обходчик эти URL не найдёт;
  нужен шаблон «на каждый день × каждую службу» (≈ 9 URL/день). Плюс robots `Disallow: /worships/?date=*&`
  и Clean-param `worship`.
- Есть `?print=1` (дубль), `/worships/personal/` (личный кабинет), `/worships/calendar/` (Disallow).
- Связанные: `https://azbyka.ru/bogosluzhebnye-ukazaniya?date=2026-10-05` (тоже по датам).

## 10. Остальные разделы (п. 3.g)

- **/molitvoslov/**, **/deti/**, **/shemy/**, (вероятно **/katehizacija/, /propovedi/, /parkhomenko/,
  /zdorovie/, /stenyaev/**) — WP + плагин bg-forreaders: блок `<div class="bg_forreaders">` с прямыми
  ссылками анониму:
  `https://azbyka.ru/molitvoslov/wp-content/uploads/bg_forreaders/<slug>_<postID>.epub?v=1788423768`
  (`200`, `application/octet-stream`, 775 КБ) и `.pdf`; `/deti/…/bg_forreaders/<slug>_37248.pdf`.
  Шаблон имени: `<slug поста>_<ID поста>.{pdf,epub,fb2}`. Все — robots Disallow.
- **/fiction/** — в HTML книги блока bg_forreaders для анонима нет; `…/bg_forreaders/alaya-chuma_53865.pdf`
  → 404, сам каталог → 403. Видимо, скачивание только для вошедших (не проверяли). Текст книги —
  на странице целиком, оглавление якорями `#ch_0_1`.
- **/kliros/** — ежедневные «Последования» `/kliros/posledovanie-tekst-sluzhby-<дата-слаг>/` с
  PDF/DOCX: `https://azbyka.ru/kliros/wp-content/uploads/2026/10/7-oktyabrya-2026.-sreda.-pervomuchenicze-fekly.-36-chas-liturgiya.pdf`
  (`200 application/pdf`, 786 КБ; в именах бывают `á`, `ó` — обходчик нормализует в `%C3%A1`).
  Ноты: `/kliros/wp-content/uploads/2022/12/blazheny-*.pdf` (десятки PDF на страницу). Robots разрешает.
- **/art/**, **/foto/** — картинки на том же хосте `azbyka.ru/<раздел>/wp-content/uploads/…`, с
  cache-busting `?v=<unixtime>`. В `srcset` есть **оригинал** (без суффикса `-WxH`) и 2–4 уменьшенные
  копии (`-300x200`, `-768x1024`, `-1024x768`). На `/foto/golgofo-raspyatskij-skit/` 350 ссылок на
  уменьшенные и 113 на оригиналы ⇒ миниатюры утраивают трафик картинок. `<image:loc>` карт: recept 9 544,
  parkhomenko/foto 5 154, deti 3 674, life 3 055, корень 3 494, fiction 2 524 …
- **/palomnik/** — MediaWiki 1.40.0, `articlepath=/palomnik/$1`, `script=/palomnik/index.php`.
  `api.php` открыт (не в robots): `?action=query&meta=siteinfo&siprop=statistics` →
  pages 85 928, **articles 16 675, images 66 749**, edits 257 494. `list=allpages` работает
  (`apcontinue`). `index.php?title=Special:AllPages` → `301 /palomnik/Special:AllPages`;
  `/palomnik/Служебная:Все_страницы` → 200 (но robots `Disallow /palomnik/Служебная:*`).
  Пространства: Файл(6), Шаблон(10), Категория(14), Свойство(102), Форма(106), smw — Semantic MediaWiki.
  Есть ещё блог `/palomnik/blogs/` (WP).
- **/quotes/** — 29 109 цитат, пагинация `/quotes/page/2/ … /page/706/`; `/quotes/*?` Disallow.
- **/vopros/** — `/vopros/<slug>/` (≈ 9 500 в картах), пагинация `/vopros/page/N/?utm_*` (utm
  выбрасываются), `/vopros/questions/ask/` (форма, Disallow), `/vopros/questions/tags/`.
- **/pravo/** — 783 записи (каноны, `/pravo/vasiliya-velikogo-92/`).
- **/test/**, **/viktorina/**, **/recept/** (пагинация до `/recept/page/408/`), **/news/** (`/news/page/314`).
- **/imeniny** — одна большая страница (256 КБ), ссылки в `/days/menology/*`, `/imya`.
- **/oktoix** — это статья энциклопедии (описание Октоиха), не сам богослужебный текст.
- **Энциклопедия в корне**: ≈ 3 400 статей `https://azbyka.ru/<slug>` (WP `post` корневого сайта,
  `X-WP-Total: 3482`), индекс — `https://azbyka.ru/dictionary` (315 КБ, ≈ 1 894 ссылки) и буквы
  `/dictionary/01 … /dictionary/28`; плюс `/1/<slug>` (≈ 10). Полный перечень — корневой
  `post-sitemap1..3.xml` (3 517 URL).

## 11. Ловушки и дубли (п. 3.j)

| Шаблон | Пример | Что делать |
|---|---|---|
| Бесконечный календарь дней | `/days/2099-12-31`, `/days/1990-01-01` (200) | фильтр дат уже есть |
| Чтения дня в Библии | `/biblia/days/2026-10-04` → вчера/завтра | добавить в `date_filter_patterns` |
| Богослужения по датам | `/worships/?date=…&worship=…` | шаблон вместо обхода, фильтр дат есть |
| Богослужебные указания | `/bogosluzhebnye-ukazaniya?date=2026-10-05` | добавить в `date_filter_patterns` |
| Год календаря | `/days/calendar/2030` | `_YEAR_SEG_RE` фильтрует — ок |
| Версия для печати | `?print=1` на всех WP-страницах (`/worships/?print=1`, `/oktoix?print=1`) | уже в exclude |
| Обратная связь с URL | `/obratnaja-svjaz?the_url=https%3A%2F%2Fazbyka.ru%2F…` — на каждой странице | **exclude** `[?&]the_url=` (сейчас спасает только Clean-param) |
| Подгрузка пагинации | `/<раздел>/page/2/?content_only=1` (из inline-JS) | **exclude** `[?&]content_only=` |
| Пагинация WP | `/quotes/page/706/`, `/recept/page/408/`, `/news/page/314` | конечна, ок |
| Комментарии | `/*/comment-page-N` | exclude (в robots Disallow) |
| Ленты | `/feed/`, `/feed/rss/`, `/feed/atom/`, `/comments/feed/`, `?feed=rss2`, `/dictionary/01/feed` | exclude есть, добавить `[?&]feed=` |
| Библия: без перевода, стихи, колонки | `?Mt.1` (=`?Mt.1&r`), `?Lk.5:8`, `?Lk.12:16-22&r`, `?Mt.1&r~c`, `?Mt.1&ru` | см. §14: оставить только `?<Кн>.<N>&<код>` |
| AJAX Отечника | `/otechnik/ajax/book/load-chapter/8979/1` | exclude `^https://azbyka\.ru/otechnik/ajax` |
| Аннотации | `/otechnik/book/annotation/10013` | exclude (robots Disallow) |
| Личные кабинеты | `/otechnik/profile`, `/otechnik/profile/settings`, `/otechnik/settings/css`, `/worships/personal/`, `/biblia/personal/favorites`, `/kliros/personal` | exclude `/(personal|profile|settings)(/|$)` |
| Вход | `//azbyka.ru/auth?reflink=<текущий URL>` на каждой странице, `/biblia/login?return_to=`, `/days/login` | уже в exclude |
| Поиск | `/video/?s=…`, `/otechnik/search`, `/<раздел>/yandex_search`, `/sear/` | `?s=` в exclude; добавить `yandex_search`, `google_search` |
| Служебные хосты | `https://stat.azbyka.ru/rate`, `wss://stat.azbyka.ru/updates`, `https://ajax.azbyka.ru/bibrefs` (из inline-JS) | exclude хосты |
| Двойной слэш | `/otechnik/Mefodij_Kulman//svjatootecheskoe-tolkovanie…/` | можно схлопывать `//`→`/` в пути |
| Cache-busting | `?v=1781257733` у картинок, `?ver=…` у css/js, `?v=` у epub/m4b | безопасно отбрасывать `ver`, `v` (для `wp-content` и `assets`) |
| Уменьшенные копии | `…-300x200.jpg`, `…-768x1024.jpg` | по желанию: не качать `-\d+x\d+\.(jpe?g|png|webp)` |
| Шаринг | `vk.ru/share.php`, `connect.ok.ru/offer`, `t.me/share`, `api.whatsapp.com`, `whatsapp://` | вне области — отсекаются |
| oEmbed | `/parkhomenko/wp-json/oembed/1.0/embed?url=…` | `/wp-json/` уже в exclude |
| MediaWiki | `/palomnik/index.php?title=…&action=…`, `Служебная:`, `Файл:`, `Участник:` | exclude есть; `Файл:` страницы — по желанию (66 749 шт.) |
| Тесты | `/test/*?question*`, `/test/gettest.php` | robots Disallow; добавить в exclude |
| Сессии в URL | не обнаружено (ни `PHPSESSID`, ни `sid=`) | — |

## 12. Оценка объёмов (п. 3.k)

| Раздел | Страниц (HTML) | Файлы | Объём (оценка) |
|---|---:|---|---|
| /otechnik/ | 21 тыс. книг ⇒ 400–700 тыс. стр. | EPUB+PDF+сканы только после входа | HTML 40–60 ГБ (несжатый); файлы: не оценить анонимно |
| /biblia/ | 75 тыс. глав×переводов + 34,7 тыс. толкований + 15 тыс. Стронг/подстрочник | 5 PDF/FB2/DOC; аудио глав на media | HTML ≈ 3–5 ГБ; аудио Синод. 1 361 × ~3–5 МБ ≈ 5–7 ГБ на перевод |
| /audio/ | 1 393 | mp3 на media.azbyka.ru (сотни на альбом, 3–35 МБ), m4b по 0,5–2 ГБ | mp3 — сотни ГБ (по выборке 1 альбом = 56 × ≈30 МБ ≈ 1,7 ГБ; 1 165 альбомов ⇒ порядка 0,5–1 ТБ); m4b — ещё столько же (дубль) |
| /video/ (PeerTube) | 1 554 | 6 267 роликов, 2 978 ч | 0,4–1,2 ТБ в зависимости от качества |
| /days/ | 16 800 дней (2000–2045) + 7 671 | иконы, mp3 «аудиокалендаря» | HTML ≈ 1–2 ГБ |
| /palomnik/ | 16 675 статей + 1 249 категорий | 66 749 файлов-картинок | картинки — десятки ГБ |
| /quotes/ | 29 109 + пагинация 706 | — | < 1 ГБ |
| /kliros/ | 2 735 | PDF/DOCX (сотни КБ – 1 МБ каждый, тысячи) | единицы–десятки ГБ |
| Прочие WP-разделы | ≈ 45 тыс. | PDF/EPUB bg_forreaders, картинки (`<image:loc>` ≈ 50 тыс. оригиналов + миниатюры) | десятки ГБ |

Итого HTML порядка **0,7–1 млн страниц**; при 1 запросе/с — **8–12 суток** только на страницы.
Медиа (без видео) — порядка 1 ТБ, с видео — 1,5–2,5 ТБ.

## 13. Пробный обход живого сайта (шаг 4)

Файлы: `crawl-test/status-run1.txt`, `log-run1-tail300.log`, `console-run1.log` (прогон 1),
`status-run2.txt`, `log-run2-tail300.log`, `console-run2.log` (прогон 2), конфиги `crawl.toml`, `crawl2.toml`.
Темп в обоих прогонах снижен до `min_delay = 1.0` (требование вежливости ≤ 1 запрос/с).

**Прогон 1** — `crawl --data /tmp/azr-test --max-minutes 20`, конфиг по умолчанию (+ min_delay 1.0):
- 1 189 скачано, 399 МБ, в очереди 152 114, ошибок 1, 0 «нужен вход», 0 пауз/429. Ровно ≈ 1,0 запрос/с.
- Посев: 16 898 адресов (16 802 дня 2000-01-01…2045-12-31 + старты + карты из robots).
- **За 20 минут не скачано ни одной HTML-страницы**: 389 карт сайта, затем 798 картинок
  `/wp-content/uploads/…` (355 МБ) и 2 PDF. Причина — баг приоритетов (§14, п.1): картинки из
  `<image:loc>` карт сайта получили приоритет карты = 1. В очереди 12 061 картинка с приоритетом 1.
- Единственная ошибка: `https://azbyka.ru/kliros/post-sitemap1.xml` → **HTTP 500** (6 попыток; у меня
  curl'ом — 200 и пустой ответ). Карта сломана на стороне сайта ⇒ ≈ 1 000 записей /kliros/ есть
  только через обход ссылок.
- robots отсёк 95 (`/shemy/schemes/*.jpg` — **картинки пособий /shemy/ запрещены robots**, 85 шт.),
  exclude — /forum и /znakomstva (их карты).

**Прогон 2** — 10 минут, старт с 13 конкретных страниц разделов, карты исключены, `seed_days=false`:
- 551 скачано (110 МБ), 8 × 404, 7 ошибок, 4 редиректа, в очереди 7 320.
- Классификация верная: все `kind=page` — `text/html`; `media` — PDF/DOC/DOCX/ZIP/FB2
  (`/biblia/downloads/bibliya.fb2` пришёл как `application/octet-stream` — сохранён по расширению, ок).
- **311 из 551 запросов ушли на /biblia/**, из 4 905 адресов /biblia/ в очереди почти все —
  варианты стихов `?1Chron.10:12&r`, `?Lk.5:8`, `?1Chron.1` (без перевода). Это главный «пожиратель»
  обхода; query_cap 400 000 их не сдерживает. 4 из 8 404 — битые ссылки на стихи
  (`/biblia/?Ps.16:33-35`, `/biblia/?Is.27:35`).
- Сверка ссылок (образец страницы vs база): у `/molitvoslov/akafist-…mechevu….html` 105 ссылок в
  области, нет в базе 11 — все либо в exclude (feed, print, auth), либо отсечены robots
  (bg_forreaders epub/pdf), либо мусор из JS (`/molitvoslov/'+full_img_href[src]+'`). У `/biblia/?Mt.1&r`
  234 ссылки, нет 4 (mp3 на media.azbyka.ru — robots; login/register — exclude). Т.е. извлечение
  ссылок работает, потери — от robots.
- mp3: 315 ссылок `/audio/audio1/zachala/Jn/Jn.20:1-10.mp3` и т.п. отсечены robots; `tube.azbyka.ru`,
  `media.azbyka.ru`, `stat.azbyka.ru`, `ajax.azbyka.ru` — по robots.txt (у всех `Disallow: /`).
- Мусор из inline-JS (text_links): `https://azbyka.ru/molitvoslov/wp-*.php`, `…/worships/wp-*.php`
  (из JSON speculationrules `"href_matches":"/wp-*.php"`), каталоги `…/wp-content/themes/azbyka-molitvoslov`
  (301→`/`), `…/wp-content/plugins/Bg-Personal/` (403), `…/'+full_img_href[src]+'`.
- `HTTP 405` на несуществующие favicon'ы `/worships/wp-content/themes/azbyka-worships/assets/images/favicons/android-icon-*.png`
  (сервер так отвечает на отсутствующие файлы) — обходчик сдаётся после 6 попыток; можно считать 405 = 404.
- `embeds` (для команды video) засорён: `https://connect.ok.ru/offer?url=…` (кнопка «поделиться»),
  `https://vk.com/share.php?url=`, `https://www.youtube.com/embed/` (шаблон из JS) — потому что
  `ok.ru`, `vk.com`, `youtube.com` в `embed_hosts` ловят любые ссылки, а не только iframe.
  Настоящий пример: `https://www.youtube.com/embed/kWVVj9Tbd-Y` из `/molitvoslov/molitva-gospodnya-otche-nash.html`.
- `/worships/personal` попал в очередь (нет в exclude). `/biblia/days/2026-10-04` в очереди (нет фильтра дат).
- В `status` сабдомены azbyka.ru отображаются как «внешн: media.azbyka.ru» хотя они в области — косметика.

## 14. Найденные проблемы обходчика (код не менял)

1. **Приоритет картинок из карт сайта = 1** (`crawler._admit_into`: `asset` наследует
   `parent_priority`, а у sitemap он 1). ⇒ десятки тысяч картинок качаются раньше любых страниц.
   Исправить: для ссылок из `kind == "sitemap"` не наследовать приоритет (брать `priority_for(url)` или
   default), а `<image:loc>` можно вообще ставить в конец (`value 40`).
2. **M3U с `Content-Type: application/octet-stream`** не разбирается: в `_finish_page`
   `should_parse` требует ctype из `PARSE_CTYPES`. Нужно разрешать разбор по расширению `.m3u/.m3u8`.
3. **respect_robots = true отрезает все mp3/m4b/epub/bg_forreaders и хосты media/tube** (§2) —
   решение за владельцем; технически нужен режим «robots только для HTML» или allow-список.
4. **Clean-param применяется целиком**, включая `worship` (ломает /worships/) и глобальные
   `file url mode time option preview off save` (§2).
5. `date_filter_patterns` не покрывает `/biblia/days/` и `/bogosluzhebnye-ukazaniya?date=`.
6. Нет исключений для `stat.azbyka.ru`, `ajax.azbyka.ru` (сейчас спасает только их robots `Disallow: /`;
   при `respect_robots=false` они попадут в обход), `/otechnik/ajax/`, `[?&]the_url=`,
   `[?&]content_only=`, `/personal`, `/profile`, `/settings/css`, `yandex_search`, `google_search`.
7. Варианты стихов Библии не ограничены (см. §13) — нужен exclude на `?…:…` и `~`.
8. `text_links` по inline-JS даёт мусор (`wp-*.php`, `'+…+'`, каталоги тем): стоит игнорировать URL
   со `*`, `'`, `+'` и пути, оканчивающиеся на каталог `wp-content/(themes|plugins)/<x>/?`.
9. `embed_hosts` ловит кнопки «поделиться» (`connect.ok.ru/offer`, `vk.com/share.php`) и голые
   `youtube.com/embed/` — фильтровать по пути (`/embed/<id>`, `/video_ext.php`, `/play/embed/`).
10. `alias_fallback` на `azbyka.org` бесполезен (Cloudflare challenge 403).
11. `HTTP 405` на отсутствующих статических файлах стоит трактовать как 404 (без 6 повторов).
12. `probe`: urllib словил `IncompleteRead` на большом RSS — ок, но в probe нет повтора.

## 15. Рекомендации для настроек обходчика

Ниже — готовый фрагмент `config.toml` (он же лежит файлом `recommended-config.toml`, загружается `load_config` без ошибок) (регулярки проверены на примерах из этого отчёта).

```toml
[site]
# azbyka.org за Cloudflare-челленджем: алиас оставить, fallback выключить
alias_fallback = false
# мусор/кэш-бастинг (ver/v безопасно отбрасывать: проверено на css/js/jpg/epub/m4b)
add_drop_params = ["ver", "v", "the_url", "content_only", "nowprocket", "_wpnonce"]
# хосты медиа (они поддомены — уже в области, но пусть будут явно)
extra_media_hosts = ["media.azbyka.ru"]
# стартовые адреса дополнительно
add_start_urls = [
  "https://azbyka.ru/dictionary",
  "https://azbyka.ru/days/calendar",
  "https://azbyka.ru/biblia/podstrochnik",
  "https://azbyka.ru/biblia/strong/greek",
  "https://azbyka.ru/biblia/strong/hebrew",
  "https://azbyka.ru/biblia/downloads/bibliya.pdf",
  "https://azbyka.ru/biblia/downloads/bibliya.fb2",
  "https://azbyka.ru/biblia/downloads/bibliya.doc",
  "https://azbyka.ru/biblia/downloads/nz.pdf",
  "https://azbyka.ru/biblia/downloads/tsya.pdf",
]

[crawl]
min_delay = 1.0            # сайт выдержал 1 req/s без 429; 0.5 не проверяли
# respect_robots = false   # ← только по решению владельца; тогда обязательно exclude ниже
add_date_filter_patterns = [
  '^https://azbyka\.ru/biblia/days/',
  '^https://azbyka\.ru/bogosluzhebnye-ukazaniya',
]
# Тексты служб: 9 URL на день (ссылки только в JS). Фильтр дат уже покрывает /worships/.
seed_date_templates = [
  "https://azbyka.ru/worships/?date={date}&worship=vespers",
  "https://azbyka.ru/worships/?date={date}&worship=matins",
  "https://azbyka.ru/worships/?date={date}&worship=chas1",
  "https://azbyka.ru/worships/?date={date}&worship=chas3",
  "https://azbyka.ru/worships/?date={date}&worship=chas6",
  "https://azbyka.ru/worships/?date={date}&worship=liturgy",
  "https://azbyka.ru/worships/?date={date}&worship=chas9",
  "https://azbyka.ru/worships/?date={date}&worship=requests",
  "https://azbyka.ru/worships/?date={date}&worship=chants",
  "https://azbyka.ru/biblia/days/{date}",
]
# ⚠ для worships нужно отключить Clean-param `worship` (см. §14 п.4), иначе шаблоны схлопнутся.
# Разумно сузить диапазон для worships/biblia-days (например 2024–2030), иначе 46 лет × 10 = 168 тыс. URL.
add_asset_host_blocklist = ["stat.azbyka.ru", "ajax.azbyka.ru", "cackle.me", "cdn.jsdelivr.net/npm/yandex-metrica-watch"]
# embed_hosts: убрать "ok.ru" и "vk.com" (ловят кнопки «поделиться») или фильтровать по /embed/

[rules]
add_exclude = [
  # служебные сабдомены
  '^https?://(stat|ajax)\.azbyka\.ru/',
  # Отечник: AJAX-дубли глав, аннотации, кабинет, поиск
  '^https://azbyka\.ru/otechnik/(ajax|ajx|context-search|search|profile|settings|users|favor|favorites|book/annotation)(/|\?|$)',
  # скачивание Отечника без куки всё равно 302→/auth (убрать строку, если есть cookies_file)
  '^https://azbyka\.ru/otechnik/books/(download|original)/',
  # личные кабинеты всех разделов
  '(?i)/(personal|profile)(/|\?|$)',
  # Библия: стихи/диапазоны, многоколоночный режим, «с ударениями», без кода перевода
  '^https://azbyka\.ru/biblia/\?[^&]*:',
  '^https://azbyka\.ru/biblia/\?.*~',
  '^https://azbyka\.ru/biblia/\?.*&ru$',
  '^https://azbyka\.ru/biblia/\?[1-4]?[A-Za-z]+\.\d+$',
  '^https://azbyka\.ru/biblia/(search|login|personal)',
  # обратная связь, подгрузка, печать, ленты, поиск
  '^https://azbyka\.ru/obratnaja-svjaz\?',
  '(?i)[?&](content_only|feed|print)=',
  '(?i)/(yandex_search|google_search)(/|\?|$)',
  '/comment-page-\d+',
  # тесты: отдельные вопросы и служебные скрипты
  '^https://azbyka\.ru/test/(gettest|getresponse)\.php',
  '^https://azbyka\.ru/test/.*\?(question|rtf)',
  # мусор из inline-JS
  "[*']|%27|\\+%27",
  '/wp-content/(themes|plugins)/[^/]+/?$',
  # worships: кабинет/календарь (Disallow)
  '^https://azbyka\.ru/worships/(calendar|__chants)/',
  # m4b — склейки mp3 по 0,5–2 ГБ (убрать, если нужны)
  '(?i)\.m4b(\?|$)',
  # уменьшенные копии картинок WP (оригинал есть в srcset); убрать, если важна точная вёрстка
  '(?i)/wp-content/uploads/.+-\d{2,4}x\d{2,4}\.(jpe?g|png|webp)(\?|$)',
]

[[query_cap]]   # Библия после exclude: 1 361 глава × 55 переводов ≈ 75 тыс. + 34,7 тыс. /in/
pattern = '^https://azbyka\.ru/biblia/'
value = 120000
```

Порядок приоритетов, который стоит поправить в коде/конфиге:
- `<image:loc>` из карт — не 1, а ≥ 40 (п.14.1).
- `/biblia/in/?…` (толкования, 34,7 тыс.) — приоритет как у Библии (10) или 14.
- `/otechnik/` главы находятся только по оглавлению книги ⇒ корни книг (из карт) должны идти
  достаточно рано; карты Отечника — полный перечень корней.

Шаблоны «конечного перечисления» (для генератора посевов, если будет):
- Библия: для каждого `{ch}` из `https://azbyka.ru/biblia/sitemap/chapters` (вида `Gen.1`) и каждого
  `{code}` из списка §5 (кроме `ru`): `https://azbyka.ru/biblia/?{ch}&{code}`.
- Аудио Библии: `https://media.azbyka.ru/audio/biblia/{code}/{Book}/{N}.mp3` (проверен `r/Mt/1`).
- Видео: `https://tube.azbyka.ru/api/v1/videos?count=100&start={0..6200}` → `files[].fileDownloadUrl`
  или `streamingPlaylists[].files[].fileDownloadUrl` (выбирать одно разрешение).
- Паломник: `https://azbyka.ru/palomnik/api.php?action=query&list=allpages&aplimit=500&apnamespace=0&format=json`
  с `apcontinue` (≈ 34 запроса на 16 675 статей); файлы — `list=allimages&aiprop=url` (66 749).
- Карты сайта: все 51 из robots.txt достаточно (обходчик их берёт сам); дополнительные не нужны.
  Битая: `https://azbyka.ru/kliros/post-sitemap1.xml` (500).

## 16. Бюджет и вежливость

- Ручные запросы curl ≈ 600 (журнал `reqlog`: 499 + ~100 неучтённых: WP API, зеркала, PeerTube),
  встроенный probe ≈ 235, карты сайта — 362 скачанных, пробные обходы 1 191 + 570.
  Итого ≈ 2 600 запросов (чуть больше целевых 2 500) — превышение из-за второго, адресного прогона.
- Темп всё время ≈ 1 запрос/с (параллельно не более двух процессов по ~0,5–0,7 запроса/с).
- Не логинились, форм не отправляли. Страницы /forum и /znakomstva не открывались; их **карты сайта**
  (`/forum/sitemap.xml`, `/znakomstva/sitemap_index.xml` и дочерние) были прочитаны скриптом подсчёта
  карт (только XML, для полноты таблицы) — 48 768 и 1 262 URL.
