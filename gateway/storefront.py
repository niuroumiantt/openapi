"""Explicit merchandising metadata; never infer capabilities from model names."""
CATEGORIES = [
    {"id": "open", "name": "Semifly 开源模型 API", "description": "开源文本模型；写作、编程与推理是场景，不是重复商品。"},
    {"id": "claude", "name": "Claude API", "description": "Anthropic 型号专区。"},
    {"id": "openai", "name": "OpenAI API", "description": "OpenAI 型号专区。"},
    {"id": "image", "name": "Image API", "description": "图像生成与编辑，按实际商品单位计费。"},
    {"id": "video", "name": "Video API", "description": "视频生成，按实际商品单位计费。"},
]
# Reviewed deployment mapping. Unlisted routes are not advertised automatically.
# Hosted aliases with uncertain model identity/licensing await review.
MODEL_METADATA = {
    "semifly-27b": {"category": "open", "scenarios": ["writing", "coding", "analysis"]},
}


def storefront(catalogue, products):
    models = []
    for name, metadata in MODEL_METADATA.items():
        if not catalogue.get(name):
            continue
        models.append({"id": name, **metadata,
                       "offers": [p for p in products if p["model"] == name],
                       "status": "configured"})
    return {"categories": CATEGORIES, "models": models}
