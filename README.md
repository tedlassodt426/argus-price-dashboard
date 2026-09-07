# Argus Price Dashboard

本项目提供 Argus 煤炭价格台、价格趋势/价差、长协定价计算、Argus PDF 日报阅读和本地邮件附件下载。

源码仓库不包含 Argus 账号、邮箱凭据、PDF、OCR缓存或浏览器会话。跨平台迁移请看 [README-MAC.md](README-MAC.md)。

## 本机运行

```bash
python3 -m pip install -r requirements.txt
python3 argus_assistant.py
```

打开 `http://127.0.0.1:8768/`。
