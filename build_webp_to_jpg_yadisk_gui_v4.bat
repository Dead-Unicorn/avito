@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] Проверяю Python...
python --version >nul 2>&1
if errorlevel 1 (
  echo Python не найден в PATH.
  echo Установи Python с python.org и отметь "Add Python to PATH".
  pause
  exit /b 1
)

echo [2/4] Обновляю pip...
python -m pip install --upgrade pip
if errorlevel 1 (
  echo Не удалось обновить pip.
  pause
  exit /b 1
)

echo [3/4] Устанавливаю зависимости...
python -m pip install --upgrade pyinstaller pillow requests
if errorlevel 1 (
  echo Не удалось установить зависимости.
  pause
  exit /b 1
)

echo [4/4] Собираю exe...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name "WebP2JPG_YaDisk" ^
  webp_to_jpg_yadisk_gui_v4.py

if errorlevel 1 (
  echo Сборка завершилась с ошибкой.
  pause
  exit /b 1
)

echo.
echo Готово.
echo EXE-файл лежит в папке dist\WebP2JPG_YaDisk.exe
pause
