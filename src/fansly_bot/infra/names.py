"""Validation centralisee des identifiants utilisateur (noms de lots, etc.).

Principe : on valide a la FRONTIERE (creation depuis l UI, lecture depuis la
BDD si suspect, import externe), pas a chaque consommation. Le type
`BatchName` est une simple alias `str` mais l intention est de marquer dans
les signatures que le nom a deja ete valide.

Politique :
- ASCII uniquement : lettres, chiffres, underscore, tiret.
- Longueur : 1 a 64 caracteres.
- Ne peut pas commencer par un point (cache) ni par un underscore
  (sous-dossiers de service prefixes `_` reserve interne).
- Doit etre un identifiant simple (pas de '/', '\\\\', '..', NUL).

Toute violation leve `InvalidBatchNameError`.
"""

from __future__ import annotations

import re

# Type alias purement intentionnel : aucun cout runtime, mais documente
# dans les signatures que la valeur a transite par validate_batch_name.
BatchName = str

# Regex stricte : 1 a 64 caracteres parmi [A-Za-z0-9_-], debutant par
# un caractere alphanumerique (interdit les noms commencant par underscore
# ou tiret qui posent souvent des soucis CLI / FS).
_BATCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class InvalidBatchNameError(ValueError):
    """Levee quand un nom de lot ne respecte pas la politique."""


def validate_batch_name(name: str) -> BatchName:
    """Verifie qu un nom de lot est sur a utiliser pour construire des chemins.

    Retourne le nom tel quel s il est valide. Leve InvalidBatchNameError
    sinon. Le message d erreur est explicite et utilisable directement
    dans une UI.
    """
    if not isinstance(name, str):
        raise InvalidBatchNameError(
            f"Le nom de lot doit être une chaîne, reçu {type(name).__name__}"
        )
    stripped = name.strip()
    if not stripped:
        raise InvalidBatchNameError("Le nom de lot ne peut pas être vide.")
    if len(stripped) > 64:
        raise InvalidBatchNameError(
            f"Le nom de lot est trop long ({len(stripped)} caractères, "
            f"max 64)."
        )
    if not _BATCH_NAME_RE.match(stripped):
        raise InvalidBatchNameError(
            "Le nom de lot doit contenir uniquement des lettres, chiffres, "
            "tirets et underscores, et commencer par une lettre ou un chiffre."
        )
    # Defense additionnelle face a des sequences problematiques meme si le
    # regex devrait deja les exclure (ceinture + bretelles).
    forbidden_patterns = ("..", "/", "\\", "\x00")
    for pat in forbidden_patterns:
        if pat in stripped:
            raise InvalidBatchNameError(
                f"Le nom de lot contient une séquence interdite ({pat!r})."
            )
    return stripped


def is_valid_batch_name(name: str) -> bool:
    """Version non-levante de `validate_batch_name`. Pratique pour les checks UI."""
    try:
        validate_batch_name(name)
        return True
    except InvalidBatchNameError:
        return False
