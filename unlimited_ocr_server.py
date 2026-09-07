# -*- coding: utf-8 -*-
"""
Unlimited-OCR 推理服务（在 GPU 机器上运行）
============================================
以 OpenAI 兼容接口提供 baidu/Unlimited-OCR 的文档解析能力。
本机 Argus 助手只需在 config.ini 里配置 [ocr] url 指向本服务即可使用。

GPU 机器要求：
  - NVIDIA 显卡，建议显存 >= 10GB
  - Python 3.12

安装（示例，Windows/Linux 均可）：
  python -m venv ocr-env
  ocr-env\\Scripts\\activate          # Linux: source ocr-env/bin/activate
  pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu129
  pip install transformers==4.57.1 Pillow==12.1.1 matplotlib einops addict easydict pymupdf psutil

模型会自动从 Hugging Face 下载（约 6GB，也可先手动下载）：
  pip install "huggingface_hub[cli]"
  huggingface-cli download baidu/Unlimited-OCR --local-dir D:/models/Unlimited-OCR

启动：
  python unlimited_ocr_server.py --port 10000 --model baidu/Unlimited-OCR
  # 模型已下载到本地时：--model D:/models/Unlimited-OCR

接口：
  POST /v1/chat/completions   文档解析（OpenAI 兼容，非流式）
  GET  /health                健康检查
"""

import argparse
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = None
TOKENIZER = None
INFER_LOCK = threading.Lock()


def load_model(model_name):
    import torch
    from transformers import AutoModel, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        model_name, trust_remote_code=True)
    model = AutoModel.from_pretrained(
        model_name,
        trust_remote_code=True,
        use_safetensors=True,
        torch_dtype=torch.bfloat16,
    )
    model = model.eval().cuda()
    return model, tokenizer


def _read_output(output_path):
    """save_results=True 时从输出目录读取解析文本"""
    if not output_path or not os.path.isdir(output_path):
        return ""
    best, best_size = "", -1
    for root, _, files in os.walk(output_path):
        for fn in files:
            if fn.lower().endswith((".md", ".txt", ".json")):
                fp = os.path.join(root, fn)
                size = os.path.getsize(fp)
                if size > best_size:
                    best_size = size
                    try:
                        with open(fp, "r", encoding="utf-8") as f:
                            best = f.read()
                    except Exception:
                        best = ""
    return best


def infer(prompt, image_paths, image_mode):
    with INFER_LOCK:
        tmp_out = tempfile.mkdtemp(prefix="unlimited_ocr_out_")
        try:
            if len(image_paths) == 1 and image_mode != "base":
                result = MODEL.infer(
                    TOKENIZER,
                    prompt=f"<image>{prompt}",
                    image_file=image_paths[0],
                    output_path=tmp_out,
                    base_size=1024, image_size=640, crop_mode=True,
                    max_length=32768,
                    no_repeat_ngram_size=35, ngram_window=128,
                    save_results=True,
                )
            else:
                result = MODEL.infer_multi(
                    TOKENIZER,
                    prompt="<image>Multi page parsing.",
                    image_files=image_paths,
                    output_path=tmp_out,
                    image_size=1024,
                    max_length=32768,
                    no_repeat_ngram_size=35, ngram_window=1024,
                    save_results=True,
                )
            if isinstance(result, str) and result.strip():
                return result
            text = _read_output(tmp_out)
            if text:
                return text
            if isinstance(result, (list, dict)):
                return json.dumps(result, ensure_ascii=False)
            return str(result) if result else ""
        finally:
            import shutil
            shutil.rmtree(tmp_out, ignore_errors=True)


def decode_images(content):
    prompt = ""
    images = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            prompt += str(part.get("text", ""))
        elif part.get("type") == "image_url":
            url = part.get("image_url", {})
            if isinstance(url, dict):
                url = url.get("url", "")
            if isinstance(url, str) and url.startswith("data:image/"):
                b64 = url.split(",", 1)[1]
                data = base64.b64decode(b64)
                fd, tmp = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                with open(tmp, "wb") as f:
                    f.write(data)
                images.append(tmp)
    if not prompt.strip():
        prompt = "document parsing."
    return prompt, images


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "ok",
                                  "model_loaded": MODEL is not None})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", "replace")
            data = json.loads(body or "{}")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        try:
            messages = data.get("messages") or []
            content = messages[-1].get("content") if messages else []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            prompt, images = decode_images(content or [])
            if not images:
                self._send_json(400, {"error": "no image in request"})
                return
            image_mode = (data.get("images_config") or {}).get(
                "image_mode", "gundam")
            text = infer(prompt, images, image_mode)
            for p in images:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            self._send_json(200, {
                "id": "chatcmpl-unlimited-ocr",
                "object": "chat.completion",
                "created": int(__import__("time").time()),
                "model": data.get("model", "Unlimited-OCR"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text or ""},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            })
        except Exception as e:
            self._send_json(500, {"error": repr(e)})


def main():
    ap = argparse.ArgumentParser(
        description="Unlimited-OCR OpenAI-compatible inference server")
    ap.add_argument("--port", type=int, default=10000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--model", default="baidu/Unlimited-OCR")
    args = ap.parse_args()

    global MODEL, TOKENIZER
    print(f"[unlimited-ocr] 加载模型 {args.model} ...", flush=True)
    MODEL, TOKENIZER = load_model(args.model)
    print("[unlimited-ocr] 模型就绪。", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[unlimited-ocr] 服务地址: http://{args.host}:{args.port}", flush=True)
    print("[unlimited-ocr] POST /v1/chat/completions  ·  GET /health", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[unlimited-ocr] 已停止。")


if __name__ == "__main__":
    main()
