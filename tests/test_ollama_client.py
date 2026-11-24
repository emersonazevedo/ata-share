"""Testa o OllamaClient contra um servidor HTTP real de mentira.

Pega a classe de bug que o report do Codex encontrou: a resposta era fechada
(saída de `with`) antes de ser lida, e `generate()` devolvia string vazia.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ata.config import OllamaConfig
from ata.summarize import OllamaClient, OllamaUnavailable


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # silêncio
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            body = json.dumps({"models": [
                {"name": "llama3.2:3b", "details": {"family": "llama"}, "capabilities": ["completion"]},
                {"name": "nomic-embed-text", "details": {"family": "nomic-bert"}},
            ]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
        for token in ('{"a"', ": 1", "}"):
            self.wfile.write((json.dumps({"response": token, "done": False}) + "\n").encode())
            self.wfile.flush()
        self.wfile.write((json.dumps({"response": "", "done": True}) + "\n").encode())


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _client(base):
    return OllamaClient(OllamaConfig(base_url=base, request_timeout_seconds=5))


def test_generate_accumulates_streamed_tokens(server):
    out = _client(server).generate("llama3.2:3b", "oi", num_ctx=2048)
    assert out == '{"a": 1}'          # regressão do bug "resposta fechada -> string vazia"
    assert json.loads(out) == {"a": 1}


def test_list_models_and_names(server):
    client = _client(server)
    assert client.model_names() == ["llama3.2:3b", "nomic-embed-text"]
    assert len(client.list_models()) == 2


def test_generate_unavailable_raises(server):
    with pytest.raises(OllamaUnavailable):
        OllamaClient(OllamaConfig(base_url="http://127.0.0.1:1", request_timeout_seconds=1)).generate(
            "x", "y", num_ctx=1024)
