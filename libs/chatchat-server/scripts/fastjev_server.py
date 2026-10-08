"""Run the local FastJev System One service used by automatic Agent routing."""

from __future__ import annotations

import argparse
import threading

import uvicorn
from fastjev import FastJev, LlamaCppBackend, SystemOneAdapter
from fastjev.http import create_app


class _LockedAdapter:
    """llama.cpp owns mutable inference state, so serialize evaluations."""

    def __init__(self, adapter: SystemOneAdapter):
        self._adapter = adapter
        self._lock = threading.Lock()
        self.served_model = adapter.served_model

    def evaluate(self, payload: dict) -> dict:
        with self._lock:
            return self._adapter.evaluate(payload)

    def models(self) -> dict:
        return self._adapter.models()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--served-model", default="fastjev-qwen3.5-4b-q4-k-m")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8811)
    parser.add_argument("--max-input-tokens", type=int, default=2048)
    parser.add_argument("--n-ctx", type=int, default=4096)
    parser.add_argument("--n-batch", type=int, default=512)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    backend = LlamaCppBackend.from_pretrained(
        args.model_path,
        args.revision,
        max_input_tokens=args.max_input_tokens,
        n_ctx=args.n_ctx,
        n_batch=args.n_batch,
        n_gpu_layers=-1,
        prefix_reuse=True,
    )
    adapter = SystemOneAdapter(
        FastJev(backend),
        served_model=args.served_model,
        description="Local Qwen3.5-4B FastJev intent router",
        release_date="2026-10-08",
    )
    app = create_app(_LockedAdapter(adapter))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
