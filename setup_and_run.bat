@echo off
chcp 65001 >nul
echo ========================================
echo   Bitrix24 Report Bot - Установка
echo ========================================
echo.

:: Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ОШИБКА: Python не найден. Установите Python 3.10+ с python.org
    pause
    exit /b 1
)

:: Создаём .env если нет
if not exist .env (
    copy .env.example .env
    echo Создан файл .env — заполните его перед запуском!
    notepad .env
    pause
)

:: Устанавливаем зависимости
echo Установка зависимостей...
pip install -r requirements.txt

echo.
echo Запуск бота...
python bot.py
pause
