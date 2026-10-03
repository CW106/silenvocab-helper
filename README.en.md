# SilenVocab Cheat Helper

[繁體中文說明](README.md)

An unofficial Windows cheat and automation helper for [SilenVocab](https://silenvocab.com/). It captures a screen area you select, uses OCR to read the word and answer choices, and shows a suggested answer. You can enable automatic answer clicking. It also offers a separate game-screen selection and optional automatic game actions. OCR and dictionary matching can be wrong, so check the result yourself.

The English launch option translates the app's controls and status messages. The game's Chinese answer choices and OCR targets stay in Chinese so recognition still works.

## Installation

You need Windows, Python, and working English and Traditional Chinese Windows OCR language support.

```powershell
python -m pip install -r requirements.txt
python vocab_helper.py --english
```

On a PC with Python installed, you can also double-click `Launch SilenVocab Helper (English).bat`. It starts the English interface with `pythonw`, so no terminal window appears. Close any running Chinese version first; only one copy can run at a time.

## Usage

1. Open a vocabulary question on [silenvocab.com](https://silenvocab.com/). Click **① Select question area** and drag around the word and all answer choices.
2. Click **② Start**. To let the app click answers, check **Auto-click answers**.
3. For automatic game actions, select the game screen with **Select game screen** and check **Auto-play game**.
4. Press **F8** to stop immediately.

`dict_cache.json` and `known_answers.json` are included translation and answer caches. The app updates them while it runs. Diagnostic logs, answer logs, screenshots, and backups are excluded from the public repository.

This project is not affiliated with the developers of SilenVocab.

