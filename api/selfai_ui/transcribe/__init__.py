"""Self-hosted STT (self.transcribe) router support.

Turns the self-hosted STT backend from a single fixed-model loader into a
multi-model router — the STT analog of the self.llamolotl text-inference
router. This package holds the model-listing contract (R1 of
``cavekit-audio-transcribe-router.md``): which models are downloaded and ready
to serve vs. which known catalog entries are merely available to pull.

Pull / swap / delete (R2–R5) and the auth-ready credential slot (R6) are later
tasks and deliberately not implemented here.
"""
