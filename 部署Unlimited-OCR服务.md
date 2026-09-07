# 部署 Unlimited-OCR 服务（GPU 机器）

`baidu/Unlimited-OCR` 是百度开源的文档解析 OCR 大模型（约 3B 参数），
可以把日报 PDF 页面直接"读懂"成带版面结构的文本（标题 / 正文 / 表格分开），
比普通文字提取准确得多，对扫描件、图表、双栏排版也有效。

**注意：这个模型必须 NVIDIA GPU 才能实用运行（建议显存 ≥10GB）。**
本机 Argus 助手默认用的是 Umi-OCR（Windows 离线 OCR 软件，自带 PaddleOCR
引擎，开箱即用）；RapidOCR 作为自动备用。
本文件是"可选升级"路线：把识别引擎换成 Unlimited-OCR 需要一台 GPU 机器。

---

## 一、在 GPU 机器上部署

### 1. 安装 Python 3.12 环境

```bat
python -m venv ocr-env
ocr-env\Scripts\activate        :: Linux: source ocr-env/bin/activate
```

### 2. 安装依赖（CUDA 12.9 版 PyTorch）

```bat
pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu129
pip install transformers==4.57.1 Pillow==12.1.1 matplotlib einops addict easydict pymupdf psutil
```

如果显卡是 Hopper（H100/H800）等，CUDA 12.9 的 PyTorch 也可用；老显卡用
对应 CUDA 版本的 PyTorch 即可（如 cu121），模型本身兼容。

### 3. 下载模型（约 6GB，首次自动下载也可以）

```bat
pip install "huggingface_hub[cli]"
huggingface-cli download baidu/Unlimited-OCR --local-dir D:/models/Unlimited-OCR
```

### 4. 启动推理服务

把 `unlimited_ocr_server.py` 复制到 GPU 机器上，然后：

```bat
python unlimited_ocr_server.py --port 10000 --model baidu/Unlimited-OCR
:: 模型已下载到本地时：
python unlimited_ocr_server.py --port 10000 --model D:/models/Unlimited-OCR
```

看到 `模型就绪` 和 `服务地址: http://0.0.0.0:10000` 即成功。
验证：

```bat
curl http://127.0.0.1:10000/health
```

防火墙需放行 10000 端口，并确认本机能访问 GPU 机器的 IP。

> 备选：如果 GPU 机器是 Linux 且内存较大，也可以用官方推荐的 SGLang /
> vLLM 部署（OpenAI 兼容接口一样），端口和模型名配置好即可。

---

## 二、让本机 Argus 助手使用该服务

1. 用记事本打开 `config.ini`，把 `[OCR]` 段的 `url` 取消注释并填上服务地址：

   ```ini
   [OCR]
   backend = unlimited
   url = http://GPU机器IP:10000
   ```

2. 重启 Argus 助手（双击 `启动Argus助手.bat`）。

3. 打开日报 → 新闻页，标题右侧会显示 `识别：Unlimited-OCR` 标记，
   说明已经走 OCR 识别。

换回本地轻量 OCR：把 `backend` 改回 `rapid`（或删掉），重启助手即可。

---

## 三、说明

- OCR 只用于新闻文字识别；价格表仍按原图显示，不受影响。
- 首次打开某天日报时，OCR 解析需要一点时间（GPU 上约 1-3 分钟整份），
  结果会缓存，之后秒开。
- 如果 OCR 服务暂时不可用，助手会自动回退到本地 PDF 文字提取，不会报错。
- 想强制重新识别某天日报，可在地址栏访问：
  `http://127.0.0.1:8768/api/report?date=2026-08-10&kind=cdi&force=1`（需已登录）。
