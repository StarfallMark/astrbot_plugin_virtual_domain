"""与 UI、传输层无关的虚拟域核心实现。"""

from .models import WorldMode, WorldStatus
from .providers import ModelConfig, ModelProviderError, ModelResponse, provider_from_config
from .protocol import Envelope, ProtocolError
from .service import VirtualDomainService
from .simulation import SimulationInput, SimulationResult, WorldSimulationEngine

__all__ = [
    "VirtualDomainService",
    "WorldMode",
    "WorldStatus",
    "ModelConfig",
    "ModelProviderError",
    "ModelResponse",
    "provider_from_config",
    "Envelope",
    "ProtocolError",
    "SimulationInput",
    "SimulationResult",
    "WorldSimulationEngine",
]
