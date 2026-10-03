# SilenVocab Cheat Helper

[繁體中文說明](README.md)

An unofficial Windows cheat and automation helper for [SilenVocab](https://silenvocab.com/). It captures a screen area you select, uses OCR to read the word and answer choices, and shows a suggested answer. You can enable automatic answer clicking. It also offers a separate game-screen selection and optional automatic game actions. OCR and dictionary matching can be wrong, so check the result yourself.

The app's interface and the game's answer choices are in Chinese; this is an English guide to the current app.

## Installation

You need Windows, Python, and working English and Traditional Chinese Windows OCR language support.

```powershell
python -m pip install -r requirements.txt
python vocab_helper.py
```

On a PC with Python installed, you can also double-click `啟動單字小幫手.bat`. It starts the app with `pythonw`, so no terminal window appears.

## Usage

1. Open a vocabulary question on [silenvocab.com](https://silenvocab.com/). Click **① 框選區域** (Select question area) and drag around the word and all answer choices.
2. Click **② 開始** (Start). To let the app click answers, check **自動點答案** (Auto-click answers).
3. For automatic game actions, select the game screen with **框選遊戲畫面** (Select game screen) and check **自動打遊戲** (Auto-play game).
4. Press **F8** to stop immediately.

`dict_cache.json` and `known_answers.json` are included translation and answer caches. The app updates them while it runs. Diagnostic logs, answer logs, screenshots, and backups are excluded from the public repository.

This project is not affiliated with the developers of SilenVocab.

