@echo off
REM Run NoteCraft straight from source - no build needed.
python -m pip install --quiet -r requirements.txt
python notecraft.py
