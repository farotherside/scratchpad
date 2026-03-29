# lexeng - Lexical Entropy Engine
# Initialization of the lexical processing subsystem.

from .corpus import CorpusMatrix
from .engine import EntropyEngine
from .mutator import LexicalMutator
from .pipeline import TransductionPipeline

__version__ = "0.1.0"
__all__ = ["CorpusMatrix", "EntropyEngine", "LexicalMutator", "TransductionPipeline"]
