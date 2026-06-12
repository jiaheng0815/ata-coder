# -*- coding: utf-8 -*-
"""
Tests for server.py — SessionStore, AgentAPIHandler, create_server.
"""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ata_coder.server import SessionStore
from ata_coder.config import AppConfig, AgentConfig, LLMConfig


class TestSessionStore:
    """SessionStore initialization and core operations."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a SessionStore with temp workspace."""
        config = AppConfig(
            llm=LLMConfig(api_key="test-key", base_url="http://test", model="test-model"),
            agent=AgentConfig(workspace_dir=str(tmp_path)),
        )
        store = SessionStore()
        return store, config

    def test_init(self):
        """SessionStore should initialize with empty sessions."""
        store = SessionStore()
        assert store.list_sessions() == []

    def test_create_returns_tuple(self, store):
        """create() should return (session_id, CoderAgent)."""
        store, config = store
        sid, agent = store.create(config)
        assert isinstance(sid, str)
        assert len(sid) == 12
        assert agent is not None

    def test_create_adds_to_list(self, store):
        """After create, list_sessions should contain the session."""
        store, config = store
        sid, _ = store.create(config)
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == sid

    def test_get_existing(self, store):
        """get() should return the agent for an existing session."""
        store, config = store
        sid, agent = store.create(config)
        assert store.get(sid) is agent

    def test_get_missing(self, store):
        """get() should return None for a non-existent session."""
        store, _ = store
        assert store.get("nonexistent") is None

    def test_get_or_create_existing(self, store):
        """get_or_create() with valid sid should return existing session."""
        store, config = store
        sid, agent = store.create(config)
        sid2, agent2 = store.get_or_create(sid, config)
        assert sid2 == sid
        assert agent2 is agent

    def test_get_or_create_new(self, store):
        """get_or_create() with None sid should create new session."""
        store, config = store
        sid, agent = store.get_or_create(None, config)
        assert sid is not None
        assert agent is not None

    def test_update_meta(self, store):
        """update_meta() should update session metadata."""
        store, config = store
        sid, _ = store.create(config)
        store.update_meta(sid, messages=5, tool_calls=3)
        meta = store.get_meta(sid)
        assert meta["messages"] == 5
        assert meta["tool_calls"] == 3

    def test_delete_existing(self, store):
        """delete() should remove an existing session."""
        store, config = store
        sid, _ = store.create(config)
        assert store.delete(sid) is True
        assert store.get(sid) is None

    def test_delete_missing(self, store):
        """delete() should return False for non-existent session."""
        store, _ = store
        assert store.delete("nonexistent") is False

    def test_thread_safety(self, store):
        """Multiple threads should be able to create sessions concurrently."""
        store, config = store
        results = []

        def create_session():
            sid, agent = store.create(config)
            results.append(sid)

        threads = [threading.Thread(target=create_session) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert len(set(results)) == 10  # all unique

    def test_skill_activation(self, store):
        """create() with skill param should activate the skill."""
        store, config = store
        with patch("ata_coder.skills.get_skill_manager") as mock_get_skill:
            mock_mgr = MagicMock()
            mock_get_skill.return_value = mock_mgr
            sid, agent = store.create(config, skill="debugger")
            mock_mgr.activate.assert_called_once_with("debugger")


class TestCreateServer:
    """create_server function."""

    def test_create_server_returns_http_server(self):
        """create_server() should return an HTTPServer instance."""
        from ata_coder.server import create_server
        from http.server import HTTPServer

        config = AppConfig(
            llm=LLMConfig(api_key="test-key", base_url="http://test", model="test-model"),
            agent=AgentConfig(workspace_dir=str(Path.cwd())),
        )
        server = create_server(config, "127.0.0.1", 0)  # port 0 = random
        assert isinstance(server, HTTPServer)
        server.shutdown()


class TestAgentAPIHandler:
    """AgentAPIHandler HTTP endpoint tests."""

    @pytest.fixture
    def config(self):
        return AppConfig(
            llm=LLMConfig(api_key="test-key", base_url="http://test", model="test-model"),
            agent=AgentConfig(workspace_dir=str(Path.cwd())),
        )

    def test_health_endpoint(self, config):
        """GET /health should return 200 with status ok."""
        from http.server import HTTPServer
        from ata_coder.server import create_server
        import urllib.request

        server = create_server(config, "127.0.0.1", 0)
        port = server.server_address[1]

        def serve():
            server.handle_request()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        time.sleep(0.1)

        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
            data = json.loads(resp.read().decode())
            assert resp.status == 200
            assert data["status"] == "ok"
        finally:
            server.shutdown()

    def test_404_on_unknown(self, config):
        """GET /unknown should return 404."""
        from http.server import HTTPServer
        from ata_coder.server import create_server
        import urllib.request
        import urllib.error

        server = create_server(config, "127.0.0.1", 0)
        port = server.server_address[1]

        def serve():
            server.handle_request()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        time.sleep(0.1)

        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/unknown")
            assert exc.value.code == 404
        finally:
            server.shutdown()
