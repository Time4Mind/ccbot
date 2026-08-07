"""Translation tables used by :mod:`ccbot.i18n`.

Locale modules contain data only; language selection and fallback behavior stay
in the stable ``ccbot.i18n`` facade.
"""

from .en import EN
from .ru import RU
from .zh import ZH

__all__ = ["EN", "RU", "ZH"]
