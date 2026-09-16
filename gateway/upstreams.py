"""Model catalogue: which public model name goes to which upstream, at what weight.

Config file (GATEWAY_CONFIG, default upstreams.json):

{
  "upstreams": {
    "local":    {"base_url": "http://127.0.0.1:11434/v1"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"}
  },
  "models": {
    "semifly-27b":   {"upstream": "local",    "model": "qwen3.8:27b-mxfp8", "weight": 1},
    "deepseek-chat": {"upstream": "deepseek", "model": "deepseek-chat",     "weight": 2}
  }
}

`weight` is how many points one token of that model costs. Real upstream keys
are read from the environment and never leave the gateway.
"""
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    public_name: str
    upstream: str
    base_url: str
    model: str
    weight: float
    api_key: str


class Catalogue:
    def __init__(self, routes: dict[str, Route]):
        self.routes = routes

    @classmethod
    def load(cls) -> "Catalogue":
        path = os.environ.get("GATEWAY_CONFIG", "upstreams.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        else:  # single local Ollama, same behaviour as v1
            model = os.environ.get("GATEWAY_MODEL", "")
            base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/v1"
            cfg = {"upstreams": {"local": {"base_url": base}},
                   "models": {model: {"upstream": "local", "model": model, "weight": 1}} if model else {}}
        routes = {}
        for name, m in cfg.get("models", {}).items():
            up = cfg["upstreams"][m["upstream"]]
            routes[name] = Route(
                public_name=name, upstream=m["upstream"], base_url=up["base_url"].rstrip("/"),
                model=m.get("model", name), weight=float(m.get("weight", 1)),
                api_key=os.environ.get(up.get("api_key_env", ""), "") if up.get("api_key_env") else "",
            )
        return cls(routes)

    def names(self) -> list[str]:
        return list(self.routes)

    def get(self, public_name: str) -> Route | None:
        return self.routes.get(public_name)

    def public(self) -> list[dict]:
        return [{"id": r.public_name, "upstream": r.upstream, "weight": r.weight} for r in self.routes.values()]
