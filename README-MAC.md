# Argus 价格台：Mac 接手说明

## 目录约定

源码可以放在任意目录；日报 PDF 和历史数据单独放在本机，例如：

```text
~/ArgusPublications/
~/ArgusPriceDashboard/
```

启动前设置：

```bash
export ARGUS_REPORT_ROOT="$HOME/ArgusPublications"
export ARGUS_CONFIG="$HOME/ArgusPriceDashboard/config.ini"
python3 argus_assistant.py
```

浏览器打开 `http://127.0.0.1:8768/`。

## Mac 接手日期

本次接手日期为 **2026-09-07**。自动下载只从该日期开始检查新邮件，不回补更早邮件；历史 `ArgusPublications` 目录原样复制保留。

下载器使用 IMAP 和 Python 标准库，不依赖 Windows。邮箱密码不要写进 GitHub；在 Mac 上用钥匙串保存，再由启动脚本临时注入 `ARGUS_IMAP_PASSWORD`。

## 迁移顺序

1. 从私有 GitHub 克隆源码。
2. 复制原 `config.ini` 到 Mac 的安全路径，并设置 `ARGUS_CONFIG`。
3. 复制整个 `ArgusPublications` 目录。
4. 复制本地 M42 数据文件 `argus_output/mcc_m42_weekly_series.csv`。
5. 安装依赖：`python3 -m pip install -r requirements.txt`。
6. 配置 macOS `launchd` 每天运行下载器，再启动价格台。

不要把 `config.ini`、邮箱凭据、Argus PDF、OCR缓存或浏览器 profile 提交到 GitHub；即使仓库是私有的，也将它们作为本机数据迁移。

首次接手时，可以先手动跑一次确认下载链路：

```bash
cd ~/ArgusPriceDashboard
cp tools/argus-download.example.json tools/argus-download.json
# 编辑 tools/argus-download.json，把 /Users/yourname 改成实际用户名
export ARGUS_IMAP_USER="你的邮箱账号"
security add-generic-password -U -s argus-imap -a "$ARGUS_IMAP_USER" -w
python3 tools/run_argus_download_mac.py --config tools/argus-download.json --first-run
```

配置中的 `handover_date` 已设为 `2026-09-07`：首次运行只从接手日开始检查新邮件，不会回补接手日前的邮件；历史 `ArgusPublications` 文件夹原样复制保留。
