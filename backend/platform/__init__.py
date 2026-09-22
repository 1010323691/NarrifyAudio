"""Persistent multi-user platform primitives for NarrifyAudio.

The legacy audio routes remain in ``backend/api`` while new user-scoped APIs
are introduced under ``/api/v1``.  Keeping the boundary explicit makes the
migration incremental without treating process memory or workspace JSON as a
source of truth.
"""

