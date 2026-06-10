"""Tiny LLM server for testing MetalBox. Uses MLX with SmolLM-135M (~270MB)."""
import time

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
MODEL = None
TOKENIZER = None
LOAD_TIME = None


class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: int = 100


def _load():
    global MODEL, TOKENIZER, LOAD_TIME
    if MODEL is not None:
        return
    import mlx_lm
    start = time.time()
    MODEL, TOKENIZER = mlx_lm.load("mlx-community/SmolLM-135M-4bit")
    LOAD_TIME = round(time.time() - start, 1)
    print(f"[tiny-llm] model loaded in {LOAD_TIME}s")


@app.get("/healthz")
def healthz():
    return {"ok": True, "models_loaded": MODEL is not None, "load_time": LOAD_TIME}


@app.post("/generate")
def generate(req: GenerateRequest):
    _load()
    import mlx_lm
    start = time.time()
    response = mlx_lm.generate(
        MODEL, TOKENIZER, prompt=req.prompt, max_tokens=req.max_tokens, verbose=False,
    )
    elapsed = round(time.time() - start, 2)
    return {"prompt": req.prompt, "response": response, "time_s": elapsed}
