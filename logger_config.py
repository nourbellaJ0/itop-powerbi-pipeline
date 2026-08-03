"""
logger_config.py — Configuration centralisée du système de logs.

Chaque module du projet appelle setup_logger(__name__) pour obtenir un logger
configuré avec :
  - Rotation de fichier  (logs/itop_pipeline.log, max 10 Mo, 5 archives)
  - Sortie console (stderr)
  - Format horodaté identique pour chaque handler

SECURITE : Ne jamais passer un token, un mot de passe ou une donnée personnelle
à un message de log. Ce module n'effectue aucun filtrage automatique.
"""

import logging
import logging.handlers
from pathlib import Path


def setup_logger(
    name: str,
    log_level: str = "INFO",
    log_dir: str = "logs",
) -> logging.Logger:
    """
    Crée et retourne un logger nommé avec handlers fichier + console.

    Args:
        name:      Nom du logger (passer __name__ depuis chaque module).
        log_level: Niveau de log : DEBUG, INFO, WARNING, ERROR, CRITICAL.
        log_dir:   Répertoire de destination des fichiers de log.

    Returns:
        Instance logging.Logger configurée.
    """
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    log_file = log_dir_path / "itop_pipeline.log"

    logger = logging.getLogger(name)
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Eviter les doublons de handlers si le logger est déjà configuré
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handler fichier avec rotation : 10 Mo par fichier, 5 archives conservées
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    # Handler console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
