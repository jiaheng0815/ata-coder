# -*- coding: utf-8 -*-
"""Backward-compat re-export — EventQueue lives in core.queue now."""
from .core.queue import EventQueue  # noqa: F401

__all__ = ["EventQueue"]
