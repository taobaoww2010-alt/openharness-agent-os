#!/usr/bin/env python3
"""测试远程 GPU 服务器连接 - 直接使用 requests"""

import requests
import json


def main():
    print("\n" + "=" * 60)
    print("🔌 测试远程 GPU 服务器连接")
    print("=" * 60)

    host = "192.168.31.164"
    port = 8080
    api_key = "tomle2026"

    print(f"\n服务器: {host}:{port}")
    print(f"API Key: {api_key}")

    url = f"http://{host}:{port}/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    data = {
        "model": "qwen3vl-8b",
        "messages": [
            {"role": "user", "content": "你好，用一句话介绍自己"}
        ],
        "temperature": 0.7,
        "max_tokens": 200
    }

    print("\n发送请求...")

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()

        result = response.json()
        print(f"\n状态码: {response.status_code}")
        print(f"响应: {json.dumps(result, indent=2, ensure_ascii=False)[:500]}...")

        if "choices" in result and len(result["choices"]) > 0:
            content = result["choices"][0]["message"]["content"]
            print(f"\n✅ 远程 GPU 服务器连接成功!")
            print(f"\nAI 回复:\n{content}")

    except requests.exceptions.RequestException as e:
        print(f"\n❌ 连接失败: {e}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
