# WebP -> JPG + Яндекс Диск: готовый набор под Windows

Внутри:
- `webp_to_jpg_yadisk_gui_v4.py` — основная программа
- `requirements.txt` — зависимости
- `build_webp_to_jpg_yadisk_gui_v4.bat` — локальная сборка `.exe` на Windows
- `.github/workflows/build-windows-exe.yml` — автоматическая сборка Windows `.exe` через GitHub Actions

## Вариант 1. Собрать `.exe` на Windows

1. Скопируй все файлы из этой папки на Windows.
2. Запусти двойным кликом `build_webp_to_jpg_yadisk_gui_v4.bat`.
3. Готовый файл появится здесь:

`dist\\WebP2JPG_YaDisk.exe`

## Вариант 2. Собрать `.exe` с Mac через GitHub Actions

Это вариант без установки Python на Windows.

1. Создай новый GitHub-репозиторий.
2. Залей в него содержимое этой папки целиком.
3. Открой вкладку **Actions**.
4. Запусти workflow `Build Windows EXE`.
5. После завершения открой запуск workflow и скачай artifact `WebP2JPG_YaDisk-exe`.
6. Внутри будет готовый `WebP2JPG_YaDisk.exe`.

## Как запустить Python-версию для отладки

```bash
python webp_to_jpg_yadisk_gui_v4.py
```

## Что делает программа

- Конвертирует `.webp` в `.jpg`
- Загружает `.jpg` в нужную папку на Яндекс Диск
- Может пропускать уже существующие файлы, если выключена перезапись
- Может собирать ссылки только на файлы, загруженные в текущем запуске

## Важно

- Для работы нужен OAuth-токен Яндекс Диска
- Нативный Windows `.exe` нужно собирать в Windows-среде. GitHub Actions с `windows-latest` решает это без ручной Windows-машины.
