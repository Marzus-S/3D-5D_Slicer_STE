# 3D-5D_Slicer_STE

Решение кейса Росатом / Stereotech на хакатоне «Открой#Моспром»: плагин **AI Assistant** для STE Slicer.
Это чат-бот на базе LLM, который подбирает режим печати 3D/5D, материал, профиль и настройки, ориентирует модель,
запускает слайсинг и объясняет свои решения. Инструменты слайсера доступны боту через встроенный MCP-сервер.

## Структура

```
plugin/      исходный код плагина и README (установка, настройка URL и ключа LLM)
results/     результаты по тестовым моделям: project.3mf, result.gcode, скриншоты чата
```

Установка, настройка и архитектура описаны в [plugin/README.md](plugin/README.md).

## Данные кейса

В репозиторий не входят, скачиваются отдельно:

- STE Slicer 7.10.12 (win64): https://storage.yandexcloud.net/software.stereotech.org/steslicer/stable/Stereotech%20STE%20Slicer-7.10.12-win64.exe
- Тестовые модели с техническими заданиями: https://storage.yandexcloud.net/software.stereotech.org/%D0%9F%D1%80%D0%B8%D0%BC%D0%B5%D1%80%D1%8B%20%D0%B4%D0%B5%D1%82%D0%B0%D0%BB%D0%B5%D0%B9.zip
- Исходники STE Slicer: https://github.com/stereotech/STE-Slicer

## Как работаем

1. Перед началом работы: `git pull`.
2. После готового кусочка: `git add -A`, `git commit -m "что сделал"`, `git push`.
3. Один файл одновременно правит один человек.
4. Ключ LLM никогда не кладём в файлы проекта: плагин хранит его в настройках слайсера.
