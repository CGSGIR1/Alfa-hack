# Публикация образа 1.1.2 в GHCR — для владельца пакета (rastok314)

Что нового в 1.1.2: отчёт берёт до 5 новостей вместо одной, в интерфейсе и
отчётах есть метка «Не является инвестиционной рекомендацией». Запуск для
пользователей не меняется: `docker compose -f compose.bundled.yaml up -d`,
в `compose.bundled.yaml` и `.env.example` уже прописан тег `1.1.2`.

Образ собран с нуля и проверен `tools/verify_public_image.py --secrets-env .env`
26.10.2026: 16 слоёв, 43 270 файлов, приватных данных и ключей не найдено.

## Что вы получаете

Файл `alfa-hack-public-1.1.2-linux-amd64.tar` (1,5 ГБ).

- SHA-256 архива: `b0f8c4476cc0c079abc0dddcbe3a544810f9338070897cae59b4d3b60dc7c4bf`
- Платформа: `linux/amd64`, метка версии `1.1.2`
- Последний слой образа: `sha256:086a69a92850beab1784dc1dd37830ac8e03262b9c6c059d13cc0325fad90bec`

## 1. Проверить архив и загрузить его в Docker

macOS / Linux:

```sh
shasum -a 256 alfa-hack-public-1.1.2-linux-amd64.tar
docker load -i alfa-hack-public-1.1.2-linux-amd64.tar
docker image inspect ghcr.io/rastok314/alfa-hack-public:1.1.2 --format '{{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .RootFS.Layers 15}}'
```

Windows (PowerShell): вместо `shasum` — `Get-FileHash alfa-hack-public-1.1.2-linux-amd64.tar -Algorithm SHA256`.

Ожидается: хеш совпадает с указанным выше, а последняя команда печатает
`linux/amd64 1.1.2 sha256:086a69a92850beab1784dc1dd37830ac8e03262b9c6c059d13cc0325fad90bec`.

## 2. Опубликовать

Нужен classic personal access token GitHub с правом `write:packages`
(GitHub → Settings → Developer settings → Personal access tokens).

```sh
docker login ghcr.io -u rastok314
docker push ghcr.io/rastok314/alfa-hack-public:1.1.2
docker logout ghcr.io
```

На запрос пароля вставьте токен. Пакет `alfa-hack-public` уже публичный,
новый тег наследует видимость, отдельно её менять не нужно. Тег `1.1.1`
не трогайте: на него можно откатиться.

## 3. Проверить после публикации

Скачивание без входа в GHCR (после `docker logout ghcr.io`):

```sh
docker image rm ghcr.io/rastok314/alfa-hack-public:1.1.2
docker pull --platform linux/amd64 ghcr.io/rastok314/alfa-hack-public:1.1.2
docker image inspect ghcr.io/rastok314/alfa-hack-public:1.1.2 --format '{{index .Config.Labels "org.opencontainers.image.version"}} {{index .RootFS.Layers 15}}'
```

Ожидается `1.1.2 sha256:086a69a92850beab1784dc1dd37830ac8e03262b9c6c059d13cc0325fad90bec`.
Если `pull` просит авторизацию — пакет приватный: GitHub → Packages →
`alfa-hack-public` → Package settings → Change visibility → Public.

Проверка запуска как у получателя (в папке репозитория, `.env` из `.env.example`):

```sh
docker compose -f compose.bundled.yaml up -d
docker compose -f compose.bundled.yaml ps
```

Ожидается `app` в состоянии `healthy`. На http://127.0.0.1:8765/ под заголовком
«Новостная аналитика» видна метка «Не является инвестиционной рекомендацией».
