from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from optimizer.nsga2 import CircuitEvaluator, NSGA2Config, NSGA2Optimizer
from schemas.design_state import DesignState


@dataclass
class OptimizerConfig:
    algorithm: str = "nsga2"
    pop_size: int = 100
    generations: int = 50
    seed: int | None = None
    verbose: bool = True


class OptimizerRegistry:
    """Factory for optimization algorithms."""

    @staticmethod
    def create(
        config: OptimizerConfig,
        state: DesignState,
        gm_id_adapter: Any,
    ) -> tuple[Any, CircuitEvaluator]:
        algorithm = config.algorithm.lower()
        if algorithm != "nsga2":
            raise ValueError(f"Unsupported optimizer algorithm: {config.algorithm}")
        evaluator = CircuitEvaluator(state, gm_id_adapter)
        nsga_config = NSGA2Config(
            pop_size=config.pop_size,
            n_generations=config.generations,
            crossover_prob=0.9,
            mutation_prob=0.15,
            seed=config.seed,
            verbose=config.verbose,
        )
        return NSGA2Optimizer(state, evaluator, nsga_config), evaluator
