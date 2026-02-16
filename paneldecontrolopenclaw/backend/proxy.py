"""Proxy server that enforces selected AI model and captures metrics.

The proxy listens on a local port and forwards incoming requests to
the configured AI provider.  Before forwarding it overrides the
``model`` field in the JSON payload to ensure the selected model is
used.  After forwarding it records metrics and stores a run record in
the database.  This allows OpenClaw to call a single local endpoint
instead of talking to different providers directly.

Currently the proxy supports OpenAI providers for the
``/v1/chat/completions`` endpoint.  Adding support for other
providers will involve branching on the provider and adjusting
endpoints and headers accordingly.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Callable, Dict, Optional

import requests


class ProxyServer(threading.Thread):
    """Threaded HTTP proxy enforcing model selection and capturing metrics."""

    def __init__(
        self,
        host: str,
        port: int,
        get_config: Callable[[], Dict[str, str]],
        log_manager,
        db,
    ) -> None:
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.get_config = get_config
        self.log_manager = log_manager
        self.db = db
        self.server: Optional[HTTPServer] = None
        # Circuit breaker state.  If consecutive errors exceed a threshold,
        # the proxy will temporarily refuse requests for a cooldown
        # interval to prevent overwhelming external providers.  These
        # values are updated in the request handler below.  See
        # ``_handle_response`` for details.
        self._error_count: int = 0
        self._breaker_until: float = 0.0

    def run(self) -> None:
        """Start the HTTP server and serve requests until shutdown."""

        class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        # Capture outer variables for use in handler
        get_config = self.get_config
        log_manager = self.log_manager
        db = self.db
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _set_headers(self, code: int = 200, content_type: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.end_headers()

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._set_headers(200)
                    self.wfile.write(b"{\"status\": \"ok\"}")
                else:
                    self._set_headers(404)
                    self.wfile.write(b"{\"error\": \"not found\"}")

            def do_POST(self) -> None:
                """Handle POST requests by forwarding to the configured AI provider and streaming replies."""
                path = self.path
                content_length = int(self.headers.get("Content-Length", 0))
                body_bytes = self.rfile.read(content_length) if content_length else b""
                run_id = str(uuid.uuid4())
                start_time = time.time()
                # Check circuit breaker state.  If the proxy is in a
                # cooldown period due to repeated errors, refuse early
                # with a 503 status and do not attempt to call the provider.
                if outer._breaker_until > 0 and time.time() < outer._breaker_until:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "service temporarily unavailable"}')
                    return
                config = get_config()
                provider: str = config.get("provider", "openai") or "openai"
                model = config.get("model")
                # Determine base URL from provider configuration.  If a provider is
                # not known, fall back to OpenAI's base URL.  Providers config is
                # expected under the top-level "providers" dictionary.
                providers_cfg: Dict[str, Dict] = config.get("providers", {}) if isinstance(config.get("providers"), dict) else {}
                provider_cfg: Dict[str, str] = providers_cfg.get(provider, {}) if providers_cfg else {}
                base_url = provider_cfg.get("base_url", "https://api.openai.com")
                # Compute target URL by concatenating base_url and path.  We
                # deliberately do not attempt to rewrite the path based on
                # provider; callers should supply the correct endpoint.  This
                # makes it possible to proxy different providers with the same
                # code, as long as the caller adjusts the path accordingly.
                target_url = f"{base_url}{path}"
                # Parse JSON body
                try:
                    payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                except Exception:
                    self._set_headers(400)
                    self.wfile.write(json.dumps({"error": "invalid JSON"}).encode("utf-8"))
                    return
                # Overwrite model
                if model:
                    payload["model"] = model
                # Determine if streaming is requested
                stream_requested = bool(payload.get("stream"))
                # Prepare headers.  Always set content type and propagate
                # inbound authentication header if provided.  If no inbound
                # header exists, use the API key from the provider
                # configuration.  Provider configuration may include
                # ``api_key``, ``api_key_header`` and ``api_key_prefix`` for
                # flexibility across different providers.
                headers = {"Content-Type": "application/json"}
                inbound_auth = self.headers.get("Authorization")
                if inbound_auth:
                    headers["Authorization"] = inbound_auth
                else:
                    # Use configured API key if present
                    api_key = provider_cfg.get("api_key") if provider_cfg else None
                    # Decode API key if encoded with prefix ENC:
                    if isinstance(api_key, str) and api_key.startswith("ENC:"):
                        try:
                            import base64
                            decoded_key = base64.b64decode(api_key[4:]).decode("utf-8")
                            api_key = decoded_key
                        except Exception:
                            api_key = None
                    api_header = provider_cfg.get("api_key_header", "Authorization") if provider_cfg else "Authorization"
                    api_prefix = provider_cfg.get("api_key_prefix", "") if provider_cfg else ""
                    if api_key:
                        headers[api_header] = f"{api_prefix}{api_key}"
                # Record run start in DB with placeholder log file.  Also write initial
                # request payload to the log for auditability (truncated to avoid excessive size).
                # Build a friendly representation of the JSON payload if possible.
                req_log = ""
                try:
                    # Try to pretty print the request payload
                    req_log = json.dumps(payload, ensure_ascii=False, indent=2)
                except Exception:
                    req_log = str(payload)
                # Truncate extremely long requests to 2000 characters
                if len(req_log) > 2000:
                    req_log = req_log[:2000] + "... [truncated]"
                # Redact potential sensitive values before logging
                def _redact(text: str) -> str:
                    # Replace API keys and tokens with placeholder
                    patterns = [
                        r"sk-[a-zA-Z0-9]{20,}",
                        r"Bearer [A-Za-z0-9\-_=]+",
                        r"api_key"\s*:\s*"[^"]+",
                    ]
                    for pat in patterns:
                        try:
                            text = re.sub(pat, "[REDACTED]", text, flags=re.IGNORECASE)
                        except Exception:
                            pass
                    return text
                req_log = _redact(req_log)
                initial_log = f"=== REQUEST ===\n{req_log}\n\n"
                log_file_path = log_manager.write_log(run_id, initial_log)
                db.add_run(run_id, provider, model or "", start_time, log_file_path)
                # Record first event: request received
                try:
                    db.add_event(run_id, start_time, "request_received", self.path)
                    # Publish event to in-memory bus for UI
                    if event_bus is not None:
                        event_bus.publish_event(run_id, "request_received", self.path, start_time)
                except Exception:
                    pass
                # Forward to provider (handle chat, embeddings, models)
                tokens_in = tokens_out = prompt_tokens = completion_tokens = total_tokens = None
                error_message = None
                response_status = 500
                end_time = start_time
                response_body_for_log = ""
                try:
                    # Generic event: request sent
                    try:
                        ts_rs = time.time()
                        db.add_event(run_id, ts_rs, "request_sent", target_url)
                        if event_bus is not None:
                            event_bus.publish_event(run_id, "request_sent", target_url, ts_rs)
                    except Exception:
                        pass
                    # Determine if this is a models list request (no JSON body required)
                    if path.startswith("/v1/models"):
                        # Simple GET forward
                        resp = requests.get(target_url, headers=headers)
                        response_status = resp.status_code
                        # Write response
                        self._set_headers(response_status)
                        try:
                            self.wfile.write(resp.content)
                            if len(response_body_for_log) < 20000:
                                response_body_for_log += resp.text
                        except BrokenPipeError:
                            pass
                        end_time = time.time()
                    elif path.startswith("/v1/embeddings"):
                        # Embeddings endpoint
                        resp = requests.post(target_url, headers=headers, json=payload)
                        response_status = resp.status_code
                        # Extract usage tokens if present
                        if resp.status_code < 500:
                            try:
                                resp_json = resp.json()
                                usage = resp_json.get("usage", {})
                                prompt_tokens = usage.get("prompt_tokens")
                                completion_tokens = usage.get("completion_tokens")
                                total_tokens = usage.get("total_tokens")
                                tokens_in = prompt_tokens
                                tokens_out = completion_tokens
                            except Exception:
                                pass
                        # Write response
                        self._set_headers(response_status)
                        try:
                            self.wfile.write(resp.content)
                            if len(response_body_for_log) < 20000:
                                response_body_for_log += resp.text
                            # Emit first_token event (embeddings returns full response at once)
                            try:
                                ts_ft = time.time()
                                db.add_event(run_id, ts_ft, "first_token")
                                if event_bus is not None:
                                    event_bus.publish_event(run_id, "first_token", None, ts_ft)
                            except Exception:
                                pass
                        except BrokenPipeError:
                            pass
                        end_time = time.time()
                    else:
                        # Chat completions or other POST endpoints
                        if stream_requested:
                            resp = requests.post(target_url, headers=headers, json=payload, stream=True)
                            response_status = resp.status_code
                            self._set_headers(response_status)
                            first_token_recorded = False
                            for chunk in resp.iter_content(chunk_size=1024):
                                if not chunk:
                                    continue
                                try:
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                                    # Limit accumulation
                                    if len(response_body_for_log) < 20000:
                                        response_body_for_log += chunk.decode("utf-8", errors="replace")
                                    # Record token chunk event (truncate details)
                                    try:
                                        token_text = chunk.decode("utf-8", errors="replace")
                                        trimmed = token_text[:100]
                                        # Record token chunk event
                                        ts_tc = time.time()
                                        db.add_event(run_id, ts_tc, "token_chunk", trimmed)
                                        if event_bus is not None:
                                            event_bus.publish_event(run_id, "token_chunk", trimmed, ts_tc)
                                    except Exception:
                                        pass
                                    # Record first token event
                                    if not first_token_recorded:
                                        try:
                                            ts_ft2 = time.time()
                                            db.add_event(run_id, ts_ft2, "first_token")
                                            if event_bus is not None:
                                                event_bus.publish_event(run_id, "first_token", None, ts_ft2)
                                        except Exception:
                                            pass
                                        first_token_recorded = True
                                except BrokenPipeError:
                                    break
                            # Record stream finished event
                            try:
                                ts_sf = time.time()
                                db.add_event(run_id, ts_sf, "stream_finished")
                                if event_bus is not None:
                                    event_bus.publish_event(run_id, "stream_finished", None, ts_sf)
                            except Exception:
                                pass
                            # Try to extract usage from headers
                            try:
                                usage = resp.headers.get("OpenAI-Usage")
                                if usage:
                                    usage_data = json.loads(usage)
                                    prompt_tokens = usage_data.get("prompt_tokens")
                                    completion_tokens = usage_data.get("completion_tokens")
                                    total_tokens = usage_data.get("total_tokens")
                                    tokens_in = prompt_tokens
                                    tokens_out = completion_tokens
                            except Exception:
                                pass
                            end_time = time.time()
                        else:
                            resp = requests.post(target_url, headers=headers, json=payload)
                            response_status = resp.status_code
                            # Extract usage tokens if present
                            if resp.status_code < 500:
                                try:
                                    resp_json = resp.json()
                                    usage = resp_json.get("usage", {})
                                    prompt_tokens = usage.get("prompt_tokens")
                                    completion_tokens = usage.get("completion_tokens")
                                    total_tokens = usage.get("total_tokens")
                                    tokens_in = prompt_tokens
                                    tokens_out = completion_tokens
                                except Exception:
                                    pass
                            self._set_headers(response_status)
                            try:
                                self.wfile.write(resp.content)
                                if len(response_body_for_log) < 20000:
                                    response_body_for_log += resp.text
                                # Record first token/event for non-stream
                                try:
                                    ts_ft3 = time.time()
                                    db.add_event(run_id, ts_ft3, "first_token")
                                    if event_bus is not None:
                                        event_bus.publish_event(run_id, "first_token", None, ts_ft3)
                                except Exception:
                                    pass
                            except BrokenPipeError:
                                pass
                            end_time = time.time()
                    # End of try block
                except Exception as exc:
                    error_message = str(exc)
                    end_time = time.time()
                    self._set_headers(500)
                    try:
                        self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
                    except BrokenPipeError:
                        pass
                    # Record error event
                    try:
                        db.add_event(run_id, end_time, "error", str(exc))
                    except Exception:
                        pass
                # Append response to log.  Truncate long responses for safety.
                if response_body_for_log:
                    # Limit to 20000 characters
                    resp_log = response_body_for_log
                    if len(resp_log) > 20000:
                        resp_log = resp_log[:20000] + "... [truncated]"
                    # Redact sensitive values before writing the response log
                    def _redact(text: str) -> str:
                        patterns = [
                            r"sk-[a-zA-Z0-9]{20,}",
                            r"Bearer [A-Za-z0-9\-_=]+",
                            r"api_key"\s*:\s*"[^"]+",
                        ]
                        for pat in patterns:
                            try:
                                text = re.sub(pat, "[REDACTED]", text, flags=re.IGNORECASE)
                            except Exception:
                                pass
                        return text
                    resp_log = _redact(resp_log)
                    final_log = f"=== RESPONSE ===\n{resp_log}\n\n"
                    log_manager.write_log(run_id, final_log)
                # Update run in DB
                db.update_run(
                    run_id,
                    end_time=end_time,
                    status="success" if error_message is None and response_status < 500 else "error",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    error_message=error_message,
                )
                # Record finish event
                try:
                    db.add_event(run_id, end_time, "request_finished", str(response_status))
                except Exception:
                    pass
                # Circuit breaker: update error count and cooldown.  If the
                # response indicates a server-side error (>= 500) or an
                # exception occurred, increment the error counter.  On
                # success reset the counter.  When the counter exceeds
                # the threshold, activate the breaker for a cooldown
                # period to prevent hammering the provider during
                # outages.  Both the threshold and cooldown duration
                # could be configurable in future via config.
                try:
                    is_err = (error_message is not None) or (response_status >= 500)
                    if is_err:
                        outer._error_count += 1
                    else:
                        outer._error_count = 0
                    # If error count reaches 5 consecutive errors, trip the breaker
                    # for 30 seconds.  Reset error count after tripping.
                    if outer._error_count >= 5:
                        outer._breaker_until = time.time() + 30.0
                        outer._error_count = 0
                except Exception:
                    pass

            # Silence default logging from BaseHTTPRequestHandler
            def log_message(self, format: str, *args: str) -> None:
                return

        # Create and start server
        self.server = ThreadedHTTPServer((self.host, self.port), Handler)
        try:
            self.server.serve_forever()
        except Exception:
            # Server terminated
            pass

    def shutdown(self) -> None:
        """Shut down the proxy server."""
        if self.server:
            self.server.shutdown()
