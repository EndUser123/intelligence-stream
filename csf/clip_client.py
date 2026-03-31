"""CLIP visual tagger for tagging video frames with visual categories."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Lazy imports - only import when model is actually needed
_clip_model = None
_model_lock = threading.Lock()

DEFAULT_CANDIDATE_LABELS = [
    "code screenshot",
    "diagram",
    "chart",
    "slide",
    "architecture drawing",
    "UI flow",
    "text overlay",
    "person speaking",
    "demo",
]

CONFIDENCE_THRESHOLD = 0.25


def _get_clip_model() -> tuple:
    """Load and return the CLIP model singleton (process-level).

    Returns:
        Tuple of (model, preprocess) from clip.load().
    """
    global _clip_model
    if _clip_model is None:
        with _model_lock:
            # Double-check after acquiring lock
            if _clip_model is None:
                import clip

                model, preprocess = clip.load("ViT-B/32", device="cpu")
                _clip_model = (model, preprocess)
    return _clip_model


def _score_image(image_path: Path, candidate_labels: list[str]) -> set[str]:
    """Score a single image against candidate labels using CLIP.

    Args:
        image_path: Path to the image file.
        candidate_labels: List of label strings to score against.

    Returns:
        Set of labels that exceeded the confidence threshold.
    """
    try:
        import clip
        import torch
        from PIL import Image

        model, preprocess = _get_clip_model()

        image = Image.open(image_path).convert("RGB")
        image_input = preprocess(image).unsqueeze(0).cpu()

        text_inputs = torch.cat(
            [clip.tokenize(label) for label in candidate_labels]
        ).cpu()

        with torch.no_grad():
            image_features = model.encode_image(image_input)
            text_features = model.encode_text(text_inputs)

            image_features /= image_features.norm(dim=-1, keepdim=True)
            text_features /= text_features.norm(dim=-1, keepdim=True)

            similarity = (image_features @ text_features.T).squeeze()

        threshold = CONFIDENCE_THRESHOLD
        matched = set()
        for i, score in enumerate(similarity):
            if score.item() > threshold:
                matched.add(candidate_labels[i])

        return matched

    except Exception:
        return set()


def tag_frames(
    image_paths: list[Path],
    candidate_labels: list[str] | None = None,
    timeout_per_image: float = 30.0,
) -> list[str]:
    """Tag video frames with visual categories using CLIP.

    Args:
        image_paths: List of paths to image files.
        candidate_labels: List of candidate label strings. Defaults to DEFAULT_CANDIDATE_LABELS.
        timeout_per_image: Timeout in seconds per image. Defaults to 30.0.

    Returns:
        Deduplicated list of labels that exceeded confidence threshold across any frame.
        Returns [] on any failure or timeout (non-fatal).
    """
    if candidate_labels is None:
        candidate_labels = DEFAULT_CANDIDATE_LABELS

    if not image_paths:
        return []

    all_labels: set[str] = set()

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_path = {
            executor.submit(_score_image, path, candidate_labels): path
            for path in image_paths
        }

        for future in future_to_path:
            try:
                matched = future.result(timeout=timeout_per_image)
                all_labels.update(matched)
            except TimeoutError:
                # Skip on timeout - non-fatal
                continue
            except Exception:
                # Skip on any other error - non-fatal
                continue

    return sorted(list(all_labels))
