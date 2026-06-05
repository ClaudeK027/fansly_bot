# src/fansly_bot/services/caption_picker.py
"""Selection aleatoire d'une legende.

Deux modes :
  1. Lot JSON : `pick(batch_name="ete_2026")` charge data/Captions/ete_2026.json
     et choisit une legende dans sa liste.
  2. Legacy (fallback) : si pas de batch_name fourni, pioche dans les fichiers
     .txt a la racine de data/Captions/.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import structlog

from ..config import Settings
from ..infra.names import InvalidBatchNameError, validate_batch_name

log = structlog.get_logger("services.caption_picker")

# Fallback ultime si tout est vide
_FALLBACK = "Du nouveau pour vous 💋"


class CaptionPicker:
    def __init__(self, settings: Settings) -> None:
        self._folder: Path = settings.paths.caption_folder

    def pick(self, batch_name: str | None = None) -> str:
        """Renvoie une legende aleatoire.

        Si `batch_name` est fourni, pioche dans le lot JSON correspondant.
        Sinon, fallback sur les fichiers .txt a la racine de Captions/."""
        if batch_name:
            captions = self._load_batch(batch_name)
            if captions:
                chosen = random.choice(captions)
                log.info(
                    "caption_picked_from_batch",
                    batch=batch_name,
                    length=len(chosen),
                    pool_size=len(captions),
                )
                return chosen
            log.warning("caption_batch_empty_or_missing", batch=batch_name)

        return self._pick_legacy()

    def _load_batch(self, batch_name: str) -> list[str]:
        # Defense en profondeur : valide le nom AVANT de construire le path,
        # meme s il est cense venir d une source de confiance. Empeche tout
        # path traversal via un nom forge en BDD (cf. audit B2).
        try:
            batch_name = validate_batch_name(batch_name)
        except InvalidBatchNameError as e:
            log.warning(
                "caption_batch_invalid_name",
                batch=batch_name, error=str(e),
            )
            return []
        path = self._folder / f"{batch_name}.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("caption_batch_load_error", batch=batch_name, error=str(e))
            return []
        captions = data.get("captions", [])
        if not isinstance(captions, list):
            log.warning("caption_batch_invalid_format", batch=batch_name)
            return []
        return [c.strip() for c in captions if isinstance(c, str) and c.strip()]

    def _pick_legacy(self) -> str:
        if not self._folder.is_dir():
            log.warning("caption_folder_missing", path=str(self._folder))
            return _FALLBACK
        files = [
            p for p in self._folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".txt"
        ]
        if not files:
            log.warning("caption_folder_empty", path=str(self._folder))
            return _FALLBACK
        chosen = random.choice(files)
        try:
            text = chosen.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("caption_read_error", file=str(chosen), error=str(e))
            return _FALLBACK
        if not text:
            log.warning("caption_file_empty", file=str(chosen))
            return _FALLBACK
        log.info("caption_picked_legacy", file=chosen.name, length=len(text))
        return text
