# -*- coding: utf-8 -*-
"""Example 01: Basic Chat — colored output with DeepSeek API."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

from terminal import ok, fail, info, dim, bold, heading, print_separator
from config import get_config
from llm_client import LLMClient

def main():
    config = get_config()
    print(f"\n{heading('Basic Chat')}  {dim(f'Model: {config.llm.model}')}")
    print_separator()

    client = LLMClient(config.llm)
    questions = [
        "Difference between Python list and tuple? One sentence.",
        "Write a one-line palindrome checker function.",
    ]
    for i, q in enumerate(questions, 1):
        print(f"\n{bold(f'Q{i}:')} {q}")
        print(f"{info('A:')} ", end="", flush=True)
        response = client.simple_chat(q)
        print(response)
    print(f"\n{ok('[DONE]')} Basic chat works!")

if __name__ == "__main__":
    main()
