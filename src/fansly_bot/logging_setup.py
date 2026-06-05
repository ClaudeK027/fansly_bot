# src/fansly_bot/logging_setup.py
"""Configuration du logging structure (structlog) avec sortie JSON fichier + console lisible."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

from .config import Settings


def _build_stdlib_root(level: str, log_dir: Path, max_bytes: int, backup_count: int,
                      json_to_file: bool, pretty_to_console: bool) -> None:
    """Configure le logger stdlib racine : structlog s'y branche derriere."""
    root = logging.getLogger()
    root.setLevel(level)
    # Reset : evite la duplication si load_settings est rappele
    for h in list(root.handlers):
        root.removeHandler(h)

    # Console handler — lisible humain
    if pretty_to_console:
        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(console_handler)

    # File handler rotatif — JSON
    if json_to_file:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_dir / "fansly-bot.jsonl",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(file_handler)

    # Reduit la verbosite des bibliotheques tierces
    for noisy in ("apscheduler", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def setup_logging(settings: Settings) -> structlog.stdlib.BoundLogger:
    """Configure structlog + stdlib. Renvoie un logger racine pret a l'emploi."""
    log_cfg = settings.logging

    _build_stdlib_root(
        level=log_cfg.level,
        log_dir=settings.paths.logs_dir,
        max_bytes=log_cfg.rotate_max_bytes,
        backup_count=log_cfg.rotate_backup_count,
        json_to_file=log_cfg.json_to_file,
        pretty_to_console=log_cfg.pretty_to_console,
    )

    # Processors communs : timestamp ISO, contexte, niveau, nom du logger
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Le rendu final differe selon console vs fichier.
    # On utilise ProcessorFormatter pour que stdlib et structlog partagent la pipeline.
    pre_chain = shared_processors

    # Pretty pour console, JSON pour fichier — applique en aval du handler.
    if log_cfg.pretty_to_console:
        console_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=True),
            foreign_pre_chain=pre_chain,
        )
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(
                h, logging.handlers.RotatingFileHandler
            ):
                h.setFormatter(console_formatter)

    if log_cfg.json_to_file:
        json_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=pre_chain,
        )
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.handlers.RotatingFileHandler):
                h.setFormatter(json_formatter)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    log = structlog.get_logger("fansly_bot")
    log.info(
        "logging_initialized",
        level=log_cfg.level,
        json_file=str(settings.paths.logs_dir / "fansly-bot.jsonl") if log_cfg.json_to_file else None,
    )
    return log
