"""
Client LLM tipis untuk endpoint OpenAI-compatible (default: 9router di localhost).

Model dipisah per peran (nama model/combo di 9router):
  Employee -> role "worker"   -> LLM_MODEL_WORKER   (default: worker)
  Leader   -> role "leader"   -> LLM_MODEL_LEADER   (default: manager)
  Manager  -> role "manager"  -> LLM_MODEL_MANAGER  (default: manager)

ENV:
  LLM_BASE_URL       default http://localhost:20128/v1
  LLM_MODEL_<ROLE>   override model per peran (WORKER / LEADER / MANAGER)
  LLM_MODEL          fallback lama: dipakai semua peran yang LLM_MODEL_<ROLE>-nya kosong
  LLM_API_KEY        opsional; kalau diisi dikirim sebagai Bearer token
  LLM_TIMEOUT        default 60 detik
  LLM_DISABLED=1     matikan AI sepenuhnya (parser WA jatuh ke regex)
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()


def llm_base_url() -> str:
    return os.environ.get("LLM_BASE_URL", "http://localhost:20128/v1").rstrip("/")


ROLE_DEFAULT_MODEL = {"worker": "worker", "leader": "manager", "manager": "manager"}


def llm_model(role: str = "worker") -> str:
    role = role.lower()
    if role not in ROLE_DEFAULT_MODEL:
        raise ValueError(f"Role LLM tidak dikenal: {role} (pilih: {', '.join(ROLE_DEFAULT_MODEL)})")
    return (
        os.environ.get(f"LLM_MODEL_{role.upper()}", "").strip()
        or os.environ.get("LLM_MODEL", "").strip()
        or ROLE_DEFAULT_MODEL[role]
    )


def llm_enabled() -> bool:
    return os.environ.get("LLM_DISABLED", "").strip().lower() not in {"1", "true", "yes"}


def chat_completion(messages: List[Dict[str, Any]], role: str = "worker", temperature: float = 0.0) -> str:
    """Panggil POST {base}/chat/completions pakai model sesuai peran, balikin teks jawaban."""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = requests.post(
        f"{llm_base_url()}/chat/completions",
        headers=headers,
        json={"model": llm_model(role), "messages": messages, "temperature": temperature, "stream": False},
        timeout=float(os.environ.get("LLM_TIMEOUT", 60)),
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
    content = resp.json()["choices"][0]["message"]["content"]
    if isinstance(content, list):  # sebagian provider balikin list of parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def extract_json(text: str) -> Dict[str, Any]:
    """Ambil objek JSON dari jawaban model (tahan terhadap ```json fences / teks pembuka)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def ask_json(prompt: str, image_base64: Optional[str] = None, role: str = "worker") -> Dict[str, Any]:
    """
    Kirim prompt (+ foto opsional) dan minta JSON. Kalau model/router menolak request bergambar,
    otomatis dicoba lagi tanpa gambar dan hasilnya ditandai '_image_ignored': True.
    """
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_base64:
        raw = image_base64.split(",")[-1]
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{raw}"}})

    try:
        return extract_json(chat_completion([{"role": "user", "content": content}], role=role))
    except Exception:
        if not image_base64:
            raise
    out = extract_json(chat_completion([{"role": "user", "content": [{"type": "text", "text": prompt}]}], role=role))
    out["_image_ignored"] = True
    return out
