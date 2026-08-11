"""Backend-neutral Agent Backend port.

``turns`` carries the neutral request/notification/result models exchanged
across the backend boundary; ``backend`` defines the immutable capability
matrix and the structural ``AgentBackend`` adapter protocol.
"""

from __future__ import annotations
