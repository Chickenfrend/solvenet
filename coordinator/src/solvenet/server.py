"""Local coordinator HTTP API and single verification loop."""

import argparse
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

from . import protocol_limits as limits
from .experiment_summary import markdown
from .frontier import FrontierLimit
from .problem_set import (
    EXPERIMENT_SETS,
    load,
    load_experiment_set,
    public_problem,
    public_set,
)
from .proof_context import VerificationBusy
from .sandbox import (
    DEFAULT_CONTAINER_TIMEOUT_SECONDS,
    ContainerVerifier,
    ContainerVerifierConfig,
)
from .store import (
    DEFAULT_FAILURE_CLASS,
    DEFAULT_GENERATION_TIMEOUT_SECONDS,
    DEFAULT_INITIAL_ATTEMPTS,
    DEFAULT_MAX_ASSIGNMENTS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL,
    FAILURE_CATEGORIES,
    FAILURE_CLASSES,
    MAX_ASSIGNMENTS,
    MAX_INITIAL_JOBS,
    MAX_REPAIRS,
    MAX_TASK_RESULT_BYTES,
    REJECTION_KINDS,
    Conflict,
    Store,
    validate_settings,
)
from .verifier import (
    DEFAULT_LEAN_TIMEOUT_SECONDS,
    LeanVerifier,
    LeanVerifierConfig,
    VerificationResult,
    VerificationStatus,
)

LOG = logging.getLogger(__name__)
IDENTIFIER_RE = re.compile(r"^[0-9a-f]{32}$")


def text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError(f"{field} must be a nonempty string of at most {limit} bytes")
    return value


def integer(value, field, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{field} must be an integer between 1 and {maximum}")
    return value


def initial_job_options(
    data, default_attempts=DEFAULT_INITIAL_ATTEMPTS, default_model=DEFAULT_MODEL
):
    """Parse either the legacy initial-job fields or explicit job groups."""
    if "initial_jobs" in data:
        if any(field in data for field in ("attempts", "model", "max_output_tokens")):
            raise ValueError(
                "initial_jobs cannot be combined with attempts, model, or max_output_tokens"
            )
        if data["initial_jobs"] is None:
            raise ValueError("initial_jobs must be a nonempty list of groups")
        return (
            DEFAULT_INITIAL_ATTEMPTS,
            DEFAULT_MODEL,
            DEFAULT_MAX_OUTPUT_TOKENS,
            data["initial_jobs"],
        )
    return (
        integer(data.get("attempts", default_attempts), "attempts", MAX_INITIAL_JOBS),
        text(data.get("model", default_model), "model", limits.MAX_MODEL_BYTES),
        integer(
            data.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            "max_output_tokens",
            limits.MAX_OUTPUT_TOKENS,
        ),
        None,
    )


def validate_generation(data):
    """Metadata is accepted for successes AND execution/formatting failures."""
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    for key, value in usage.items():
        if key not in ("input_tokens", "output_tokens") or (
            value is not None
            and (type(value) is not int or not 0 <= value <= 2**63 - 1)
        ):
            raise ValueError("usage must contain nonnegative token counts or null")
    generation = data.get("generation", {})
    if not isinstance(generation, dict):
        raise ValueError("generation must be an object")
    strings = {
        "raw_response": limits.MAX_RAW_RESPONSE_BYTES,
        "model": limits.MAX_MODEL_BYTES,
        "finish_reason": limits.MAX_FINISH_REASON_BYTES,
        "model_digest": 256,
    }
    numbers = {
        "total_duration_ns",
        "load_duration_ns",
        "prompt_eval_duration_ns",
        "eval_duration_ns",
        "context_length",
        "max_output_tokens",
    }
    for key, value in generation.items():
        if key in strings:
            if not isinstance(value, str) or len(value.encode()) > strings[key]:
                raise ValueError(
                    f"generation.{key} must be a string of at most {strings[key]} bytes"
                )
            if key == "model_digest" and not re.fullmatch(
                r"sha256:[0-9a-f]{64}", value
            ):
                raise ValueError("generation.model_digest must be a SHA-256 digest")
        elif key == "raw_response_truncated":
            if type(value) is not bool:
                raise ValueError("generation.raw_response_truncated must be boolean")
        elif key in ("temperature", "seed"):
            validate_settings({key: value})
        elif key in numbers:
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError(f"generation.{key} must be a nonnegative integer")
        else:
            raise ValueError(f"Unknown generation field: {key}")


def experiment_identity(data):
    allowed = {
        "idempotency_key",
        "set_id",
        "version",
        "sha256",
        "strategy",
        "model",
        "initial_jobs",
        "attempts",
        "max_repairs",
        "max_output_tokens",
        "generation_timeout_seconds",
        "max_assignments",
        "generation_settings",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"Unknown experiment fields: {', '.join(sorted(unknown))}")
    return text(data.get("idempotency_key"), "idempotency_key", 256)


def experiment_config(data, fixture):
    strategy = data.get("strategy")
    if strategy not in ("independent", "repair"):
        raise ValueError("strategy must be independent or repair")
    depth = data.get("max_repairs", 0 if strategy == "independent" else 2)
    if (
        type(depth) is not int
        or not 0 <= depth <= MAX_REPAIRS
        or (strategy == "independent" and depth != 0)
        or (strategy == "repair" and depth == 0)
    ):
        raise ValueError(
            "max_repairs must match strategy (0 for independent, 1-2 for repair)"
        )
    attempts, model, budget, groups = initial_job_options(
        data,
        default_attempts=DEFAULT_INITIAL_ATTEMPTS if strategy == "independent" else 1,
        default_model=None,
    )
    if groups is None:
        groups = [{"model": model, "count": attempts, "max_output_tokens": budget}]
    config = {
        "set_id": fixture.set_id,
        "version": fixture.version,
        "sha256": fixture.sha256,
        "environment": fixture.environment,
        "strategy": strategy,
        "initial_jobs": groups,
        "max_repairs": depth,
        "generation_timeout_seconds": integer(
            data.get("generation_timeout_seconds", DEFAULT_GENERATION_TIMEOUT_SECONDS),
            "generation_timeout_seconds",
            limits.MAX_GENERATION_TIMEOUT_SECONDS,
        ),
        "max_assignments": integer(
            data.get("max_assignments", DEFAULT_MAX_ASSIGNMENTS),
            "max_assignments",
            MAX_ASSIGNMENTS,
        ),
    }
    if "generation_settings" in data:
        config["generation_settings"] = data["generation_settings"]
    return config


def run_options(data):
    if "generation_settings" in data and data["generation_settings"] is None:
        raise ValueError("generation_settings must be an object")
    statement = text(data.get("statement"), "statement", limits.MAX_STATEMENT_BYTES)
    imports = data.get("imports", ["Init"])
    if not isinstance(imports, list) or not 1 <= len(imports) <= limits.MAX_IMPORTS:
        raise ValueError(
            f"imports must be a nonempty list of up to {limits.MAX_IMPORTS} modules"
        )
    for module in imports:
        text(module, "import", limits.MAX_IMPORT_BYTES)
    attempts, model, budget, initial_jobs = initial_job_options(data)
    return {
        "statement": statement,
        "imports": imports,
        # v1 `attempts` counts initial search chains/jobs, not
        # completed candidates in run inspection's attempts[].
        "attempts": attempts,
        "model": model,
        "max_output_tokens": budget,
        "max_repairs": data.get("max_repairs", 0),
        "generation_timeout_seconds": integer(
            data.get("generation_timeout_seconds", DEFAULT_GENERATION_TIMEOUT_SECONDS),
            "generation_timeout_seconds",
            limits.MAX_GENERATION_TIMEOUT_SECONDS,
        ),
        "max_assignments": integer(
            data.get("max_assignments", DEFAULT_MAX_ASSIGNMENTS),
            "max_assignments",
            MAX_ASSIGNMENTS,
        ),
        "initial_jobs": initial_jobs,
        "generation_settings": data.get("generation_settings"),
    }


def validate_result(data):
    validate_generation(data)
    if data.get("status") == "completed":
        if not isinstance(data.get("output"), dict):
            raise ValueError("output must be an object")
        text(data["output"].get("text"), "output.text", limits.MAX_CANDIDATE_BYTES)
        if "type" in data["output"]:
            text(data["output"]["text"], "output.text", MAX_TASK_RESULT_BYTES)
    elif data.get("status") == "failed":
        text(data.get("error"), "error", limits.MAX_ERROR_BYTES)
        failure_class = data.get("failure_class", DEFAULT_FAILURE_CLASS)
        if failure_class not in FAILURE_CLASSES:
            raise ValueError("failure_class must be transient or permanent")
        if (
            "failure_category" in data
            and data["failure_category"] not in FAILURE_CATEGORIES
        ):
            raise ValueError(
                "failure_category must be provider_failure, formatting_failure, or other_failure"
            )
    elif data.get("status") == "rejected":
        text(data.get("error"), "error", limits.MAX_ERROR_BYTES)
        if data.get("rejection_kind") not in REJECTION_KINDS:
            raise ValueError(
                "rejection_kind must be malformed_assignment or unsupported_protocol"
            )
    else:
        raise ValueError("status must be completed, failed, or rejected")


class Coordinator:
    def __init__(self, store, verifier):
        self.store = store
        self.verifier = verifier

    def ingest_group_graph_response(self, group_id, job_id):
        """Coordinator-private replay of a persisted completed graph response."""
        return self.store.ingest_group_graph_response(group_id, job_id)

    def build_group_context_packet(self, group_id, task_id, messages, **limits):
        return self.store.build_group_context_packet(
            group_id, task_id, messages, **limits
        )

    def enqueue_group_context_job(self, *args, **kwargs):
        """Explicit focused task dispatch, without choosing a scheduling policy."""
        return self.store.enqueue_group_job(*args, **kwargs, graph_context=True)

    def job_context_packet(self, job_id):
        return self.store.job_context_packet(job_id)

    def tick(self):
        self.store.expire()
        binding = None
        if self.store.needs_artifact_identity():
            try:
                identity = self.verifier.artifact_identity()
            except Exception:
                LOG.exception("Could not identify artifact verifier")
                identity = None
            binding = self.store.bind_group_artifact_verifier(identity)
        changed = self.store.advance_groups()
        artifact = (
            self.store.pending_group_artifact() if binding and binding[0] else None
        )
        if artifact:
            checked = self._check_artifact(artifact, binding)
            return changed if checked is False else True
        attempt = self.store.pending()
        if not attempt:
            return changed
        elapsed_unknown = False
        bundle = attempt.get("bundle")
        usage = {"status": "usage_unknown"}
        check_id = None
        try:
            if bundle is not None:
                if not binding or not binding[0]:
                    return changed
                bundle = bundle | {
                    "verifier_identity": binding[0],
                    "verifier_revision": binding[1],
                }
                check_id = self.store.begin_composed_check(
                    attempt["id"],
                    "attempt",
                    bundle,
                    deadline_ms=int(
                        getattr(
                            self.verifier,
                            "timeout_seconds",
                            DEFAULT_LEAN_TIMEOUT_SECONDS,
                        )
                        * 1000
                    ),
                )
                result, usage = self.verifier.verify_composed(bundle)
                after = self.verifier.artifact_identity()
                self.store.bind_group_artifact_verifier(after)
            elif attempt["requires_composed"]:
                result = VerificationResult(
                    VerificationStatus.VERIFIER_ERROR,
                    "Graph target has no immutable composed proof context",
                    0,
                )
            else:
                result = self.verifier.verify(
                    attempt["statement"],
                    attempt["candidate"],
                    imports=json.loads(attempt["imports"]),
                )
        except VerificationBusy:
            return changed
        except FrontierLimit as error:
            return (
                self.store.stop_unchecked_target(
                    attempt["id"], bundle["group_id"], str(error)
                )
                or changed
            )
        except Exception:
            LOG.exception("Verifier failed")
            result = VerificationResult(
                VerificationStatus.VERIFIER_ERROR,
                "Verifier raised an internal error; see coordinator logs",
                0,
            )
            elapsed_unknown = True
            usage = usage | {"elapsed_unknown": True}
        self.store.verified(
            attempt["id"],
            result,
            elapsed_unknown=elapsed_unknown,
            bundle=bundle,
            usage=usage,
            check_id=check_id,
        )
        return True

    def _check_artifact(self, artifact, binding):
        # Environment is an explicit local execution identity. No worker label
        # determines it; a different identity cannot be replayed by this verifier.
        if artifact["environment"] != artifact["target_environment"]:
            self.store.checked_group_artifact(
                artifact["id"],
                "incompatible",
                "Artifact environment differs from target environment",
                binding=binding,
            )
            return
        imports = json.loads(artifact["imports"])
        target_imports = json.loads(artifact["target_imports"])
        try:
            bundle = (
                self.store.ensure_artifact_context(artifact["id"], binding)
                if hasattr(self.verifier, "verify_composed")
                else None
            )
        except (ValueError, Conflict) as error:
            self.store.checked_group_artifact(
                artifact["id"], "rejected", str(error), binding=binding
            )
            return
        try:
            check_id = (
                self.store.begin_composed_check(
                    artifact["id"],
                    "artifact",
                    bundle,
                    deadline_ms=int(
                        getattr(
                            self.verifier,
                            "timeout_seconds",
                            DEFAULT_LEAN_TIMEOUT_SECONDS,
                        )
                        * 1000
                    ),
                )
                if bundle
                else None
            )
        except VerificationBusy:
            return False
        except FrontierLimit as error:
            self.store.stop_frontier(artifact["group_id"], str(error))
            return
        graph_mode = self.store.group_loop(artifact["group_id"])
        if bundle is not None and (
            bundle["declarations"] or (graph_mode and graph_mode["mode"] == "graph")
        ):
            try:
                result, usage = self.verifier.verify_composed(bundle)
            except Exception:
                LOG.exception("Composed artifact verifier failed")
                result = VerificationResult(
                    VerificationStatus.VERIFIER_ERROR,
                    "Composed artifact verifier failed",
                    0,
                )
                usage = {"status": "usage_unknown", "elapsed_unknown": True}
            self.store.record_group_lean_check(
                artifact["id"],
                result.status,
                None if usage.get("elapsed_unknown") else result.elapsed_ms,
            )
            try:
                self.store.bind_group_artifact_verifier(
                    self.verifier.artifact_identity()
                )
            except Exception:
                self.store.bind_group_artifact_verifier(None)
            self.store.checked_group_artifact(
                artifact["id"],
                str(result.status),
                result.diagnostics,
                binding=binding,
                bundle=bundle,
                result=result,
                usage=usage,
                check_id=check_id,
            )
            return
        try:

            def check(check_imports):
                try:
                    if hasattr(self.verifier, "verify_artifact"):
                        outcome = self.verifier.verify_artifact(
                            artifact["statement"],
                            artifact["proof"],
                            imports=check_imports,
                            identity=binding[0],
                        )
                    else:
                        outcome = self.verifier.verify(
                            artifact["statement"],
                            artifact["proof"],
                            imports=check_imports,
                        )
                except Exception:
                    self.store.record_group_lean_check(
                        artifact["id"], "verifier_error", None
                    )
                    raise
                self.store.record_group_lean_check(
                    artifact["id"], outcome.status, outcome.elapsed_ms
                )
                return outcome

            result = check(imports)
            if result.verified and imports != target_imports:
                # Acceptance under broader imports alone does not make a lemma
                # available to the target. Replay in the target's imports.
                result = check(target_imports)
        except Exception:
            LOG.exception("Artifact verifier failed")
            result = VerificationResult(
                VerificationStatus.VERIFIER_ERROR,
                "Verifier raised an internal error; see coordinator logs",
                0,
            )
        if isinstance(self.verifier, LeanVerifier):
            # Local project/dependencies may have changed during the two Lean
            # calls. Retry under a fresh identity rather than storing a result
            # from mixed inputs (the project is locally trusted, not hostile).
            try:
                after = self.verifier.artifact_identity()
            except Exception:
                LOG.exception("Could not re-identify local artifact verifier")
                after = None
            if after != binding[0]:
                self.store.bind_group_artifact_verifier(after)
        self.store.checked_group_artifact(
            artifact["id"],
            "verified" if result.verified else str(result.status),
            result.diagnostics,
            binding=binding,
            bundle=bundle,
            result=result,
            usage={"status": "known", "direct": [], "type": [], "transitive": []},
            check_id=check_id,
        )

    def loop(self, stop):
        while not stop.is_set():
            try:
                if self.tick():
                    continue
            except Exception:
                LOG.exception("Scheduler iteration failed")
            stop.wait(0.25)


def make_server(coordinator, address=("127.0.0.1", 8080)):  # noqa: C901 -- local handler definitions and routes
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def respond(self, code, value=None, headers=None):
            body = json.dumps(value).encode() if value is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, header_value in (headers or {}).items():
                self.send_header(name, header_value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def respond_markdown(self, value):
            body = value.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            """Keep errors generated by BaseHTTPRequestHandler on the JSON API."""
            if code == 501:
                code = 405
                message = "Method not allowed"
            message = message or self.responses.get(code, ("HTTP error",))[0]
            headers = {"Allow": "GET, HEAD, POST"} if code == 405 else None
            self.respond(code, {"error": message}, headers)

        def segments(self):
            try:
                raw_path = urlsplit(self.path).path
            except ValueError as error:
                raise ValueError("Malformed request target") from error
            raw_segments = (
                raw_path.split("/")[1:]
                if raw_path.startswith("/")
                else raw_path.split("/")
            )
            segments = []
            for raw_segment in raw_segments:
                if re.search(r"%(?![0-9A-Fa-f]{2})", raw_segment):
                    raise ValueError("Malformed percent encoding in path")
                try:
                    segment = unquote_to_bytes(raw_segment).decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError("Path must use valid UTF-8") from error
                segments.append(segment)
            return segments

        def identifier(self, value):
            return value if IDENTIFIER_RE.fullmatch(value) else None

        def pagination(self, *, offset=False, preview=False):
            query = urlsplit(self.path).query
            try:
                pairs = parse_qsl(
                    query,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=3 if preview else 2,
                    errors="strict",
                )
            except (ValueError, UnicodeDecodeError) as error:
                raise ValueError("Invalid pagination query") from error
            allowed = {"limit", "offset" if offset else "before"}
            if preview:
                allowed.add("preview")
            if len({key for key, _ in pairs}) != len(pairs) or any(
                key not in allowed for key, _ in pairs
            ):
                raise ValueError("Unknown or repeated pagination parameter")
            values = dict(pairs)
            if preview and values.get("preview", "0") not in ("0", "1"):
                raise ValueError("Invalid preview parameter")

            def number(name, default, minimum, maximum):
                raw = values.get(name)
                if raw is None:
                    return default
                if (
                    not raw.isascii()
                    or not raw.isdecimal()
                    or not minimum <= int(raw) <= maximum
                ):
                    raise ValueError(
                        f"{name} must be an integer between {minimum} and {maximum}"
                    )
                return int(raw)

            result = (
                number("limit", 20, 1, 100),
                number(
                    "offset" if offset else "before",
                    0 if offset else None,
                    0 if offset else 1,
                    2**63 - 1,
                ),
            )
            return (*result, values.get("preview") == "1") if preview else result

        def read_json(self, limit):
            raw_size = self.headers.get("Content-Length")
            if raw_size is None:
                self.respond(411, {"error": "Content-Length is required"})
                return None
            try:
                size = int(raw_size)
            except ValueError:
                self.respond(400, {"error": "Content-Length must be an integer"})
                return None
            if size <= 0:
                self.respond(400, {"error": "Body must not be empty"})
                return None
            if size > limit:
                self.respond(413, {"error": f"Body must be at most {limit} bytes"})
                return None
            try:
                data = json.loads(self.rfile.read(size))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.respond(400, {"error": "Body must be valid UTF-8 JSON"})
                return None
            if not isinstance(data, dict):
                self.respond(400, {"error": "Expected JSON object"})
                return None
            return data

        def do_GET(self):  # noqa: C901 -- explicit bounded API route dispatch
            try:
                parts = self.segments()
                if parts == ["health"]:
                    return self.respond(200, {"status": "ok"})
                if parts == ["ready"]:
                    readiness = coordinator.verifier.readiness()
                    value = {"status": "ready" if readiness.ready else "unavailable"}
                    if readiness.diagnostics:
                        value["diagnostics"] = readiness.diagnostics
                    return self.respond(200 if readiness.ready else 503, value)
                if parts == ["v1", "fixture-sets"]:
                    return self.respond(
                        200,
                        {
                            "items": [
                                public_set(load(path))
                                for _, path in sorted(EXPERIMENT_SETS.items())
                            ]
                        },
                    )
                if parts == ["v1", "model-activity"]:
                    try:
                        pairs = parse_qsl(
                            urlsplit(self.path).query,
                            keep_blank_values=True,
                            strict_parsing=True,
                            max_num_fields=32,
                            errors="strict",
                        )
                    except (ValueError, UnicodeDecodeError) as error:
                        raise ValueError("Invalid model activity query") from error
                    if not pairs or any(key != "model" for key, _ in pairs):
                        raise ValueError("Specify model identifiers")
                    models = [
                        text(value, "model", limits.MAX_MODEL_BYTES)
                        for _, value in pairs
                    ]
                    if len(set(models)) != len(models):
                        raise ValueError("Repeated model identifier")
                    return self.respond(200, coordinator.store.model_activity(models))
                if (
                    len(parts) >= 5
                    and parts[:2] == ["v1", "fixture-sets"]
                    and parts[3] == "versions"
                ):
                    try:
                        version = (
                            int(parts[4])
                            if parts[4].isascii() and parts[4].isdecimal()
                            else None
                        )
                    except ValueError:
                        version = None
                    path = EXPERIMENT_SETS.get((parts[2], version))
                    if path is not None:
                        fixture = load(path)
                        if len(parts) == 5:
                            limit, offset, show_preview = self.pagination(
                                offset=True, preview=True
                            )
                            if show_preview and limit > 10:
                                raise ValueError("preview limit must be at most 10")
                            return self.respond(
                                200,
                                public_set(fixture)
                                | {
                                    "problems": [
                                        public_problem(
                                            p, detail=show_preview, preview=show_preview
                                        )
                                        for p in fixture.problems[
                                            offset : offset + limit
                                        ]
                                    ],
                                    "next_offset": offset + limit
                                    if offset + limit < len(fixture.problems)
                                    else None,
                                },
                            )
                        if len(parts) == 7 and parts[5] == "problems":
                            problem = next(
                                (p for p in fixture.problems if p.id == parts[6]), None
                            )
                            if problem is not None:
                                return self.respond(
                                    200,
                                    public_set(fixture)
                                    | public_problem(problem, detail=True),
                                )
                            return self.respond(
                                404, {"error": "Unknown fixture problem"}
                            )
                    if len(parts) in (5, 7) and (
                        len(parts) == 5 or parts[5] == "problems"
                    ):
                        return self.respond(
                            404, {"error": "Unknown fixture set/version"}
                        )
                if parts == ["v1", "runs"]:
                    limit, before = self.pagination()
                    return self.respond(
                        200, coordinator.store.recent_runs(limit, before)
                    )
                if (
                    len(parts) == 4
                    and parts[:2] == ["v1", "proofs"]
                    and self.identifier(parts[2])
                    and parts[3] in ("bundle", "evidence")
                ):
                    bundle = coordinator.store.checked_proof_bundle(parts[2])
                    if bundle is None:
                        return self.respond(404, {"error": "Unknown composed proof"})
                    return self.respond(
                        200,
                        bundle
                        if parts[3] == "bundle"
                        else coordinator.store.composed_evidence(parts[2]),
                    )
                if (
                    len(parts) == 4
                    and parts[:2] == ["v1", "groups"]
                    and self.identifier(parts[2])
                    and parts[3] == "graph"
                ):
                    if coordinator.store.group_loop(parts[2]) is None:
                        return self.respond(404, {"error": "Unknown group"})
                    # A bounded planning neighborhood; formal reuse eligibility is
                    # conveyed by frozen packets and committed proof evidence.
                    return self.respond(
                        200, coordinator.store.group_claim_neighborhood(parts[2])
                    )
                if (
                    len(parts) == 3
                    and parts[:2] == ["v1", "groups"]
                    and self.identifier(parts[2])
                ):
                    group = coordinator.store.group(parts[2])
                    if group and any(
                        a["status"] == "verified" for a in group["artifacts"]
                    ):
                        # Archived groups are not fingerprinted on every scheduler
                        # tick. Only an inspection that needs a current label does
                        # the potentially expensive verifier identity check.
                        try:
                            identity = coordinator.verifier.artifact_identity()
                        except Exception:
                            LOG.exception(
                                "Could not identify verifier for group inspection"
                            )
                            identity = None
                        group = coordinator.store.group(
                            parts[2], verifier_identity=identity
                        )
                    loop = coordinator.store.group_loop(parts[2]) if group else None
                    return self.respond(
                        200 if loop else 404,
                        {"group": group, "loop": loop}
                        if loop
                        else {"error": "Unknown group"},
                    )
                if parts == ["v1", "experiments"]:
                    limit, before = self.pagination()
                    return self.respond(
                        200, coordinator.store.recent_experiments(limit, before)
                    )
                if (
                    len(parts) == 3
                    and parts[:2] == ["v1", "runs"]
                    and self.identifier(parts[2])
                ):
                    run = coordinator.store.run(parts[2])
                    return self.respond(
                        200 if run else 404, run or {"error": "Unknown run"}
                    )
                if (
                    len(parts) == 4
                    and parts[:2] == ["v1", "runs"]
                    and self.identifier(parts[2])
                    and parts[3] == "status"
                ):
                    status = coordinator.store.run_status(parts[2])
                    return self.respond(
                        200 if status else 404, status or {"error": "Unknown run"}
                    )
                if (
                    len(parts) == 3
                    and parts[:2] == ["v1", "experiments"]
                    and self.identifier(parts[2])
                ):
                    experiment = coordinator.store.experiment(parts[2])
                    return self.respond(
                        200 if experiment else 404,
                        experiment or {"error": "Unknown experiment"},
                    )
                if (
                    len(parts) == 4
                    and parts[:2] == ["v1", "experiments"]
                    and self.identifier(parts[2])
                    and parts[3] in ("summary", "summary.md")
                ):
                    report = coordinator.store.experiment_summary(parts[2])
                    if report is None:
                        return self.respond(404, {"error": "Unknown experiment"})
                    if parts[3] == "summary.md":
                        return self.respond_markdown(markdown(report))
                    return self.respond(200, report)
                self.respond(404, {"error": "Unknown endpoint"})
            except ValueError as error:
                self.respond(400, {"error": str(error)})
            except Exception:
                LOG.exception("HTTP request failed")
                self.respond(500, {"error": "Internal coordinator error"})

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):  # noqa: C901 -- explicit bounded API route dispatch
            try:
                parts = self.segments()
                # Candidate + raw response may both expand under JSON escaping.
                is_result = (
                    len(parts) == 4
                    and parts[:2] == ["v1", "assignments"]
                    and self.identifier(parts[2])
                    and parts[3] == "result"
                )
                limit = (
                    limits.MAX_RESULT_REQUEST_BYTES
                    if is_result
                    else limits.MAX_REQUEST_BYTES
                )
                data = self.read_json(limit)
                if data is None:
                    return
                if parts == ["v1", "experiments"]:
                    key = experiment_identity(data)
                    fixture = load_experiment_set(
                        data.get("set_id"), data.get("version"), data.get("sha256")
                    )
                    config = experiment_config(data, fixture)
                    experiment, created = coordinator.store.create_experiment(
                        key, config, fixture.problems
                    )
                    return self.respond(201 if created else 200, experiment)
                if parts == ["v1", "runs"]:
                    return self.respond(
                        201, coordinator.store.submit(**run_options(data))
                    )
                if parts == ["v1", "groups"]:
                    required = {
                        "request_key",
                        "statement",
                        "imports",
                        "environment",
                        "models",
                    }
                    if set(data) - (
                        required
                        | {
                            "max_work",
                            "deadline",
                            "model_capabilities",
                            "mode",
                            "graph_limits",
                        }
                    ):
                        raise ValueError("Unknown group field")
                    if required - set(data):
                        raise ValueError(
                            "Missing group fields: "
                            + ", ".join(sorted(required - set(data)))
                        )
                    group_id = coordinator.store.start_group_loop(**data)
                    return self.respond(
                        201,
                        {
                            "id": group_id,
                            "loop": coordinator.store.group_loop(group_id),
                        },
                    )
                if parts == ["v1", "fixture-runs"]:
                    allowed = {
                        "set_id",
                        "version",
                        "sha256",
                        "problem_id",
                        "model",
                        "attempts",
                        "max_repairs",
                        "max_output_tokens",
                        "generation_timeout_seconds",
                        "max_assignments",
                        "generation_settings",
                    }
                    if set(data) - allowed:
                        raise ValueError("Unknown fixture-run fields")
                    text(data.get("model"), "model", limits.MAX_MODEL_BYTES)
                    fixture = load_experiment_set(
                        data.get("set_id"), data.get("version"), data.get("sha256")
                    )
                    problem = next(
                        (p for p in fixture.problems if p.id == data.get("problem_id")),
                        None,
                    )
                    if problem is None:
                        raise ValueError("Unknown fixture problem")
                    options = run_options(
                        {
                            **data,
                            "statement": problem.statement,
                            "imports": list(problem.imports),
                        }
                    )
                    return self.respond(
                        201,
                        coordinator.store.submit_fixture(fixture, problem, **options),
                    )
                if parts == ["v1", "claim"]:
                    worker = text(
                        data.get("worker_id"), "worker_id", limits.MAX_IDENTIFIER_BYTES
                    )
                    models = data.get("models")
                    if (
                        not isinstance(models, list)
                        or not 1 <= len(models) <= limits.MAX_CLAIM_MODELS
                    ):
                        raise ValueError(
                            f"models must be a nonempty list of up to {limits.MAX_CLAIM_MODELS} identifiers"
                        )
                    for model in models:
                        text(model, "model", limits.MAX_MODEL_BYTES)
                    capabilities = data.get("capabilities", [])
                    if (
                        not isinstance(capabilities, list)
                        or len(capabilities) > 2
                        or any(
                            type(capability) is not str
                            or capability
                            not in ("generation_settings", "model_respond")
                            for capability in capabilities
                        )
                        or len(set(capabilities)) != len(capabilities)
                    ):
                        raise ValueError("Invalid capabilities")
                    health = data.get("provider_health")
                    health_reasons = (
                        "Ollama service unreachable",
                        "Ollama service unavailable",
                        "Ollama model list unavailable",
                        "Ollama model not installed",
                        "OpenAI credential rejected",
                        "OpenAI model unavailable",
                        "OpenAI profile or model access unsupported",
                        "OpenAI rate limited",
                        "OpenAI service unavailable",
                        "OpenAI network unavailable",
                        "OpenAI deadline exceeded",
                        "OpenAI compatibility check inconclusive",
                    )
                    unobserved_reasons = (
                        "OpenAI deadline exceeded",
                        "OpenAI compatibility check inconclusive",
                        "OpenAI rate limited (recovering via scheduled jobs)",
                        "OpenAI service unavailable (recovering via scheduled jobs)",
                        "OpenAI network unavailable (recovering via scheduled jobs)",
                    )
                    if health is not None and (
                        not isinstance(health, dict)
                        or set(health) - {"status", "reason"}
                        or health.get("status")
                        not in ("ready", "unavailable", "unobserved")
                        or (
                            "reason" in health
                            and health["reason"]
                            not in (
                                unobserved_reasons
                                if health["status"] == "unobserved"
                                else health_reasons
                            )
                        )
                        or (health["status"] == "ready" and "reason" in health)
                    ):
                        raise ValueError("Invalid provider health")
                    claim = coordinator.store.claim(
                        worker,
                        models,
                        supports_generation_settings="generation_settings"
                        in capabilities,
                        supports_model_respond="model_respond" in capabilities,
                        provider_health=health,
                    )
                    return self.respond(200 if claim else 204, claim)
                if (
                    len(parts) == 4
                    and parts[:2] == ["v1", "assignments"]
                    and self.identifier(parts[2])
                    and parts[3] in ("heartbeat", "result")
                ):
                    token = text(
                        data.get("lease_token"),
                        "lease_token",
                        limits.MAX_IDENTIFIER_BYTES,
                    )
                    if parts[3] == "heartbeat":
                        return self.respond(
                            200, coordinator.store.heartbeat(parts[2], token)
                        )
                    if parts[3] == "result":
                        validate_result(data)
                        return self.respond(
                            200, coordinator.store.result(parts[2], data)
                        )
                self.respond(404, {"error": "Unknown endpoint"})
            except Conflict as error:
                self.respond(409, {"error": str(error)})
            except ValueError as error:
                self.respond(400, {"error": str(error)})
            except Exception:
                LOG.exception("HTTP request failed")
                self.respond(500, {"error": "Internal coordinator error"})

    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("solvenet.db"))
    parser.add_argument("--project", type=Path, default=Path("lean"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--host", default="127.0.0.1", help="API bind address (default: loopback)"
    )
    parser.add_argument("--verifier", choices=("docker", "local"), default="docker")
    parser.add_argument("--image", default="solvenet-verifier:local")
    parser.add_argument(
        "--lean-timeout",
        type=float,
        default=DEFAULT_LEAN_TIMEOUT_SECONDS,
        help="total Lean verification deadline in seconds (default: 10)",
    )
    parser.add_argument(
        "--container-timeout",
        type=float,
        default=DEFAULT_CONTAINER_TIMEOUT_SECONDS,
        help="outer Docker deadline in seconds (default: 30)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        verifier_config = LeanVerifierConfig(timeout_seconds=args.lean_timeout)
    except ValueError as error:
        parser.error(str(error))
    if args.verifier == "docker":
        try:
            verifier = ContainerVerifier(
                args.image,
                verifier_config=verifier_config,
                container_config=ContainerVerifierConfig(
                    deadline_seconds=args.container_timeout,
                ),
            )
        except ValueError as error:
            parser.error(str(error))
    else:
        verifier = LeanVerifier(args.project, config=verifier_config)
    coordinator = Coordinator(Store(args.db), verifier)
    stop = threading.Event()
    thread = threading.Thread(target=coordinator.loop, args=(stop,), daemon=True)
    server = make_server(coordinator, (args.host, args.port))
    thread.start()
    LOG.info("Coordinator listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        stop.set()
        thread.join(timeout=40)


if __name__ == "__main__":
    main()
