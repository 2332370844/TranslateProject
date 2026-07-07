# FanYI

一个轻量桌面翻译小工具。打开后会显示在桌面上方，输入文字，按 `Ctrl+Enter` 或点击“翻译”即可得到译文。

## 运行

```powershell
python -m pip install -r requirements.txt
python app.py
```

也可以在 Windows 上双击 `run.bat`。

截图翻译需要额外安装 Tesseract OCR：

```powershell
winget install --id tesseract-ocr.tesseract -e
```

第一版默认识别中英混合文本，需要 Tesseract 语言包 `chi_sim` 和 `eng`。

## 功能

- 默认窗口置顶，适合作为桌面浮窗使用。
- 空闲 30 秒后自动收起成悬浮按钮，点击“译”即可展开。
- 可以点击“收起”立即变成悬浮按钮，悬浮按钮支持拖动。
- 自动判断中文转英文、英文转中文。
- 支持手动选择“英文 → 中文”“中文 → 英文”“任意语言 → 中文”。
- `Ctrl+Enter` 翻译，`Esc` 清空。
- `Ctrl+Shift+S` 框选截图翻译，结果会在选区附近弹出。
- 一键复制译文。

## 说明

当前版本优先调用 Google Translate 的公开端点，失败后回退到 MyMemory。后续如果你有 DeepL、OpenAI 或其他翻译 API key，可以只替换 `app.py` 里的翻译函数，不需要改界面。

截图翻译使用本机离线 OCR，不会上传截图；识别到的文字仍会走当前翻译接口。
