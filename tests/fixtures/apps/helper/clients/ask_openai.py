"""Ask an AIOS App with OpenAI's own client (testing guide, Part 22).

    python ask_openai.py helper "hello"            # the answer, and the run it was
    python ask_openai.py helper "hello" --stream   # the same, streamed

``AIOS_SERVER`` is where AIOS is (``http://127.0.0.1:18080`` if unset) and ``AIOS_API_KEY`` the
key to use; ``aios dev`` needs none.
"""

import argparse
import os
from typing import Any

from openai import APIStatusError, OpenAI
from openai.types.chat import ChatCompletionMessageParam


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask an AIOS App with OpenAI's own client")
    parser.add_argument("app", help="the App, which is the model")
    parser.add_argument("message")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    server = os.environ.get("AIOS_SERVER", "http://127.0.0.1:18080").rstrip("/")
    client = OpenAI(
        base_url=f"{server}/openai/v1",
        api_key=os.environ.get("AIOS_API_KEY") or "none-needed-for-aios-dev",
        timeout=300,
        max_retries=0,
    )
    print("models:", ", ".join(model.id for model in client.models.list()))
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": args.message}]
    if args.stream:
        stream = client.chat.completions.create(
            model=args.app,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                print(chunk.choices[0].delta.content, end="", flush=True)
        print()
        return
    reply = client.chat.completions.create(model=args.app, messages=messages)
    print(reply.choices[0].message.content)
    aios: dict[str, Any] = dict((reply.model_extra or {}).get("aios") or {})
    print(f"run {aios.get('run_id')}: {aios.get('status')}")


if __name__ == "__main__":
    try:
        main()
    except APIStatusError as error:  # what AIOS said, as OpenAI's clients see it
        print(f"error {error.status_code} ({error.code}): {error.message}")
        raise SystemExit(1) from None
