@echo off
chcp 65001 > nul
title 시드 실시간 감시
cd /d %~dp0
if not exist config.ini (
  echo config.ini 가 없어요. config.example.ini 를 복사해서 config.ini 로 바꾸고 채워 주세요.
  pause
  exit /b
)
python -m pip install -r requirements.txt --quiet --disable-pip-version-check
python realtime.py %*
pause
