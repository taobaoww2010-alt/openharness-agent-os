from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from daoyi.kernel import HAS_CPP_CORE

if HAS_CPP_CORE:
    import _daoyi as _CPP

logger = logging.getLogger(__name__)

SMALL_MODEL_PATH = os.environ.get(
    "DY_SMALL_MODEL",
    str(Path(__file__).parent.parent.parent.parent / "models" / "qwen3-2b-vl-instruct-q4_k_m.gguf"),
)

_IDLE_UNLOAD_SECONDS = int(os.environ.get("DY_SMALL_MODEL_IDLE_TIMEOUT", "300"))  # 5 min


@contextlib.contextmanager
def _suppress_llama_logs():
    """Temporarily suppress llama.cpp verbose output to stderr."""
    old_stderr = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)


class SmallModelClient:
    _instance: SmallModelClient | None = None

    def __init__(self, model_path: str = SMALL_MODEL_PATH) -> None:
        if not HAS_CPP_CORE:
            raise RuntimeError("C++ core not available, cannot load small model")

        self._model_path = model_path
        self._engine: Any = None
        self._loaded = False
        self._last_used: float = 0.0

    @classmethod
    def get_instance(cls, model_path: str = SMALL_MODEL_PATH) -> SmallModelClient:
        if cls._instance is None:
            cls._instance = cls(model_path)
        return cls._instance

    @classmethod
    def is_available(cls) -> bool:
        if not HAS_CPP_CORE:
            return False
        if cls._instance is not None and cls._instance._loaded:
            return True
        return bool(os.environ.get("DY_SMALL_MODEL")) or Path(SMALL_MODEL_PATH).exists()

    def ensure_loaded(self) -> None:
        # Auto-unload if idle too long (frees ~1GB when not needed)
        if self._loaded and time.time() - self._last_used > _IDLE_UNLOAD_SECONDS:
            self.unload()
        if self._loaded:
            return
        if not os.path.isfile(self._model_path):
            raise FileNotFoundError(f"Small model not found: {self._model_path}")

        logger.info("Loading small model: %s", self._model_path)
        self._engine = _CPP.create_llm_engine()
        with _suppress_llama_logs():
            self._engine.load_model(self._model_path)
        self._loaded = True
        self._last_used = time.time()

    def unload(self) -> None:
        if self._engine and self._loaded:
            with _suppress_llama_logs():
                self._engine.unload_model()
            self._loaded = False
            logger.info("Small model unloaded")

    @classmethod
    def preload(cls, model_path: str = SMALL_MODEL_PATH) -> None:
        """Start loading the small model in a background daemon thread.

        This ensures the model is ready (or at least compiling Metal shaders)
        before the first classification call, without blocking the main event loop.
        """
        if not cls.is_available():
            return
        if cls._instance is not None and cls._instance._loaded:
            return
        import threading as _threading
        _t = _threading.Thread(
            target=lambda: cls.get_instance(model_path).ensure_loaded(),
            daemon=True,
        )
        _t.start()

    def generate(self, text: str, max_tokens: int = 512, temperature: float = 0.3) -> str:
        self.ensure_loaded()
        self._last_used = time.time()
        messages = [{"role": "user", "content": text}]
        result = self._engine.infer_full(messages, [], max_tokens, temperature)
        return result.get("text", "").strip()

    _FEW_SHOT = """Classify the user's intent as one word: tool, chat, code, search, code_review, file_ops

Rules:
- Short continuation messages ("go", "继续", "然后呢") → inherit previous intent
- Weather/news/current info → search
- File manipulation (read/write/edit/delete) → file_ops
- Code writing/implementation → code
- Command execution, app launching, terminal operations → tool

Input: run ls -la in the terminal
Answer: tool
Input: Run 'echo hello' in the shell
Answer: tool
Input: execute whoami and tell me the output
Answer: tool
Input: run the command pwd
Answer: tool
Input: bash run.sh
Answer: tool
Input: 执行 ls 命令
Answer: tool
Input: 在终端里运行 echo test
Answer: tool
Input: 打开浏览器
Answer: tool
Input: 打开终端
Answer: tool
Input: 启动 nginx
Answer: tool
Input: 帮我找一下昨天的日志
Answer: tool
Input: 搜索文件 main.cpp
Answer: tool
Input: 帮我运行一个命令
Answer: tool
Input: 帮我查一下现在几点了
Answer: chat
Input: 今天天气怎么样
Answer: search
Input: 查北京天气
Answer: search
Input: 帮我查询现在的天气
Answer: search
Input: 帮我查一下北京的空气质量
Answer: search
Input: 搜索最近的新闻
Answer: search
Input: 帮我找一下某某的论文
Answer: search
Input: What is AI
Answer: chat
Input: 什么是量子计算
Answer: chat
Input: 你好
Answer: chat
Input: 你叫什么名字
Answer: chat
Input: 帮我介绍一下Python
Answer: chat
Input: 你觉得怎么样
Answer: chat
Input: 帮我创建一个新文件
Answer: code
Input: write a Python script to sort files
Answer: code
Input: write a function that calculates fibonacci
Answer: code
Input: 写一个 Python 脚本
Answer: code
Input: 查找 src/ 下所有 Python 文件
Answer: search
Input: 搜索项目中所有 TODO 注释
Answer: search
Input: search for all error logs
Answer: search
Input: 检查我的代码有没有安全漏洞
Answer: code_review
Input: review 一下 pull request
Answer: code_review
Input: 帮我审核这段代码
Answer: code_review
Input: 把 README.md 重命名为 README.old
Answer: file_ops
Input: 复制 config.json 到 backup/
Answer: file_ops
Input: 删除 /tmp/old.log
Answer: file_ops
Input: 移动所有 .txt 文件到 archive/
Answer: file_ops
Now classify:"""

    def classify(self, user_input: str) -> str:
        self.ensure_loaded()
        self._last_used = time.time()

        prompt = f"{self._FEW_SHOT}\nInput: {user_input}\nAnswer:"
        msg = [{"role": "user", "content": prompt}]
        result = self._engine.infer_full(msg, [], 8, 0.1)
        text = result.get("text", "").strip().lower()

        first_word = text.split("<")[0].split("\n")[0].strip().rstrip(". ").split()[0] if text.split() else ""
        for keyword in ["tool", "chat", "code", "search", "code_review", "file_ops"]:
            if first_word == keyword or first_word.startswith(keyword):
                return keyword
        for keyword in ["tool", "chat", "code", "search", "code_review", "file_ops"]:
            if keyword in text:
                return keyword
        return "chat"
