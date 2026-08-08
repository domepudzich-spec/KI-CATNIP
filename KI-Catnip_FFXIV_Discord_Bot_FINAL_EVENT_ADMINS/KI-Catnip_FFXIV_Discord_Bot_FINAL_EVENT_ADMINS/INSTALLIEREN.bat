@echo off
title KI-Catnip Eorzea-Enzyklopaedie - Installation
echo.
echo ===============================================
echo   KI-Catnip KI-Catnip Eorzea-Enzyklopaedie
echo ===============================================
echo.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Installation abgeschlossen.
echo.
echo WICHTIG:
echo Aktiviere im Discord Developer Portal unter Bot:
echo MESSAGE CONTENT INTENT
echo.
echo Danach .env.example zu .env kopieren und Tokens eintragen.
pause
