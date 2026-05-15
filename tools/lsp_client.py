import json
import logging
import select
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LSPLocation:
    file: str
    line: int
    character: int


class LSPClientError(Exception):
    pass


class LSPClient:
    """Minimal stdio LSP client for optional semantic impact lookups."""

    def __init__(self, command: list[str], project_root: str, timeout: float = 4.0):
        self.command = command
        self.project_root = Path(project_root).resolve()
        self.timeout = timeout
        self.process: subprocess.Popen | None = None
        self._next_id = 1

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self) -> None:
        if self.process:
            return
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.project_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        init_result = self.request("initialize", {
            "processId": None,
            "rootUri": self._path_to_uri(self.project_root),
            "capabilities": {
                "textDocument": {
                    "references": {"dynamicRegistration": False},
                    "callHierarchy": {"dynamicRegistration": False},
                }
            },
            "workspaceFolders": [{
                "uri": self._path_to_uri(self.project_root),
                "name": self.project_root.name,
            }],
        })
        if init_result is None:
            raise LSPClientError("LSP initialize returned no result")
        self.notify("initialized", {})

    def close(self) -> None:
        process = self.process
        if not process:
            return
        try:
            if process.poll() is None:
                try:
                    self.request("shutdown", None, timeout=1.0)
                    self.notify("exit", None)
                except Exception:
                    pass
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
        finally:
            self.process = None

    def did_open(self, file_path: str, language_id: str) -> None:
        path = Path(file_path).resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": self._path_to_uri(path),
                "languageId": language_id,
                "version": 1,
                "text": text,
            }
        })

    def references(self, file_path: str, line: int, character: int) -> list[LSPLocation]:
        result = self.request("textDocument/references", {
            "textDocument": {"uri": self._path_to_uri(Path(file_path).resolve())},
            "position": {"line": max(0, line - 1), "character": max(0, character)},
            "context": {"includeDeclaration": False},
        })
        return self._locations_from_result(result)

    def incoming_calls(self, file_path: str, line: int, character: int) -> list[LSPLocation]:
        items = self.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": self._path_to_uri(Path(file_path).resolve())},
            "position": {"line": max(0, line - 1), "character": max(0, character)},
        })
        locations: list[LSPLocation] = []
        if not isinstance(items, list):
            return locations

        for item in items[:5]:
            calls = self.request("callHierarchy/incomingCalls", {"item": item})
            if not isinstance(calls, list):
                continue
            for call in calls:
                caller = call.get("from", {}) if isinstance(call, dict) else {}
                loc = self._location_from_item(caller)
                if loc:
                    locations.append(loc)
        return _dedupe_locations(locations)

    def request(self, method: str, params: Any, timeout: float | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._write({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })

        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            message = self._read_message(deadline)
            if message is None:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise LSPClientError(str(message["error"]))
            return message.get("result")
        raise LSPClientError(f"LSP request timed out: {method}")

    def notify(self, method: str, params: Any) -> None:
        self._write({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

    def _write(self, payload: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise LSPClientError("LSP process is not running")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + body)
        self.process.stdin.flush()

    def _read_message(self, deadline: float) -> dict[str, Any] | None:
        if not self.process or not self.process.stdout:
            raise LSPClientError("LSP process is not running")
        if self.process.poll() is not None:
            raise LSPClientError("LSP process exited")

        headers: dict[str, str] = {}
        while time.monotonic() < deadline:
            line = self._readline(deadline)
            if not line:
                return None
            if line in {b"\r\n", b"\n"}:
                break
            decoded = line.decode("ascii", errors="ignore").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.lower()] = value.strip()

        length = int(headers.get("content-length", "0") or "0")
        if length <= 0:
            return None
        body = self.process.stdout.read(length)
        if not body:
            return None
        return json.loads(body.decode("utf-8", errors="ignore"))

    def _readline(self, deadline: float) -> bytes:
        if not self.process or not self.process.stdout:
            return b""
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([self.process.stdout], [], [], remaining)
        if not ready:
            return b""
        return self.process.stdout.readline()

    def _locations_from_result(self, result: Any) -> list[LSPLocation]:
        if not isinstance(result, list):
            return []
        locations = []
        for item in result:
            loc = self._location_from_item(item)
            if loc:
                locations.append(loc)
        return _dedupe_locations(locations)

    def _location_from_item(self, item: Any) -> LSPLocation | None:
        if not isinstance(item, dict):
            return None
        uri = item.get("uri") or item.get("targetUri")
        range_data = item.get("range") or item.get("targetSelectionRange") or item.get("selectionRange")
        if not uri or not isinstance(range_data, dict):
            return None
        start = range_data.get("start", {})
        if not isinstance(start, dict):
            return None
        return LSPLocation(
            file=str(self._uri_to_path(uri)),
            line=int(start.get("line", 0)) + 1,
            character=int(start.get("character", 0)),
        )

    def _path_to_uri(self, path: Path) -> str:
        return path.resolve().as_uri()

    def _uri_to_path(self, uri: str) -> Path:
        parsed = urlparse(uri)
        return Path(unquote(parsed.path)).resolve()


class OptionalLSPImpactProvider:
    COMMANDS = {
        "rust": [["rust-analyzer"]],
        "go": [["gopls", "serve"], ["gopls"]],
        "javascript": [["typescript-language-server", "--stdio"]],
        "typescript": [["typescript-language-server", "--stdio"]],
        "tsx": [["typescript-language-server", "--stdio"]],
        "python": [["pyright-langserver", "--stdio"], ["pyright", "--stdio"]],
        "java": [["jdtls"]],
    }

    LANGUAGE_IDS = {
        "rust": "rust",
        "go": "go",
        "javascript": "javascript",
        "typescript": "typescript",
        "tsx": "typescriptreact",
        "python": "python",
        "java": "java",
    }

    def __init__(self, project_root: str, language_detector: Any, timeout: float = 4.0):
        self.project_root = Path(project_root).resolve()
        self.language_detector = language_detector
        self.timeout = timeout
        self._command_cache: dict[str, list[str] | None] = {}

    def available_languages(self) -> list[str]:
        return sorted(language for language in self.COMMANDS if self._command_for_language(language))

    def related_locations(
        self,
        file_path: str,
        declaration_line: int,
        declaration_character: int,
        extra_positions: list[tuple[int, int]] | None = None,
    ) -> list[LSPLocation]:
        language = self.language_detector.detect_language(file_path)
        command = self._command_for_language(language or "")
        language_id = self.LANGUAGE_IDS.get(language or "")
        if not command or not language_id:
            return []

        positions = [(declaration_line, declaration_character)]
        positions.extend(extra_positions or [])
        locations: list[LSPLocation] = []

        try:
            with LSPClient(command, str(self.project_root), timeout=self.timeout) as client:
                client.did_open(file_path, language_id)
                for line, character in positions:
                    locations.extend(client.references(file_path, line, character))
                    locations.extend(client.incoming_calls(file_path, line, character))
        except Exception as exc:
            logger.debug("LSP impact lookup failed for %s: %s", file_path, exc)
            return []

        return _dedupe_locations(locations)

    def _command_for_language(self, language: str) -> list[str] | None:
        if language in self._command_cache:
            return self._command_cache[language]
        for command in self.COMMANDS.get(language, []):
            if shutil.which(command[0]):
                self._command_cache[language] = command
                return command
        self._command_cache[language] = None
        return None


def _dedupe_locations(locations: list[LSPLocation]) -> list[LSPLocation]:
    seen = set()
    unique = []
    for loc in locations:
        key = (loc.file, loc.line, loc.character)
        if key in seen:
            continue
        seen.add(key)
        unique.append(loc)
    return unique
