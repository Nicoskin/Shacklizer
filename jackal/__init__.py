"""jackal — шакалайзер фото под потерю UDP-пакетов на видеосвязи."""

from .core import Params, degrade
from .presets import DEFAULT, PRESETS

__all__ = ["Params", "degrade", "PRESETS", "DEFAULT"]
__version__ = "1.0.0"
