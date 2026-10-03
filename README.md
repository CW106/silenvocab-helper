# SilenVocab Cheat 單字小幫手

[English README](README.en.md)

適用於 [SilenVocab](https://silenvocab.com/) 的非官方 Windows cheat／自動作答輔助工具。它讀取使用者框選的網站畫面，以 OCR 辨識題目與選項，顯示查到的答案；可自行勾選自動點答案。程式也提供對戰畫面框選與自動操作選項。辨識和字典比對可能出錯，請自行確認作答結果。

## 安裝

需要 Windows、Python 及可用的中英文 Windows OCR 語言套件。

```powershell
python -m pip install -r requirements.txt
python vocab_helper.py
```

也可以在已安裝 Python 的電腦上雙擊 `啟動單字小幫手.bat`。啟動檔使用 `pythonw`，不會顯示終端視窗。

## 使用

1. 開啟 [silenvocab.com](https://silenvocab.com/) 的單字題目，按「① 框選區域」，框住題目與所有答案。
2. 按「② 開始」。需要自動點擊時，勾選「自動點答案」。
3. 如需遊戲流程操作，另行框選遊戲畫面並勾選「自動打遊戲」。
4. 按 **F8** 可緊急停止。

`dict_cache.json` 和 `known_answers.json` 是隨此版本附上的字典與答案快取。程式會在執行時更新它們。診斷紀錄、作答紀錄、截圖和備份不納入公開儲存庫。

本工具是非官方輔助程式，與 SilenVocab 網站開發者無關。

