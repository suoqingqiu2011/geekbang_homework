from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from ltm_core import cache, concept, okf_cli, project
from ltm_core.lock import file_lock
from ltm_core.types import (
    GENERATED_BY,
    MEMORY_TYPE,
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    LtmError,
    fail,
    now_iso,
    ok,
)

DEFAULTS = {
    "enabled": True,
    "okfCli": "node_modules/pi-okf/dist/cli.js",
    "allowedRoots": [],
    "scopes": {"project": {"root": ".pi/ltm"}, "global": {"root": "~/.pi/ltm"}},
    "session": {
        "injectOnSessionStart": True,
        "warmIndexOnSessionStart": True,
        "briefMaxChars": 1200,
        "maxKeysPerScope": 20,
    },
    "lock": {"timeoutMs": 5000, "staleMs": 10000, "retries": 8},
    "gate": {"validateOnWrite": True, "rollbackOnInvalid": True},
}


KINDS = ("requirement", "fact", "inference", "evidence")
LIFECYCLES = ("active", "expired", "deprecated")
_FINGERPRINT_MAX_BYTES = 2 * 1024 * 1024
_INDEX_LINK = re.compile(r"^\s*[*-]\s+\[([^\]]*)\]\(([^)]+)\)(?:\s*-\s*(.*))?$")

_SECRET_PREFIXES = (
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    (
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")),
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|token|passw(?:or)?d|pwd|authorization|bearer)\b"
    r"\s*[:=]\s*[\"']?([^\s\"',;]{8,})"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_SECRET_PLACEHOLDERS = (
    "example",
    "sample",
    "placeholder",
    "redacted",
    "changeme",
    "yourkey",
    "yourtoken",
    "yourpassword",
    "your_",
    "hashed",
    "bcrypt",
    "dummy",
    "fake",
)


def _secret_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    if _PRIVATE_KEY.search(text):
        reasons.append("private key block")
    for label, pattern in _SECRET_PREFIXES:
        if pattern.search(text):
            reasons.append(label)
    for match in _SECRET_ASSIGNMENT.finditer(text):
        value = match.group(2).strip().strip("\"'")
        lowered = value.lower()
        if any(marker in lowered for marker in _SECRET_PLACEHOLDERS):
            continue
        reasons.append(f"credential assignment: {match.group(1)}")
    return sorted(set(reasons))


def _kind_from_tags(tags: list) -> str:
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("kind:"):
            value = tag[5:].strip().lower()
            if value in KINDS:
                return value
    return ""


def _kind_from_args(args: dict, fallback: str) -> tuple[str, str | None]:
    raw = str(args.get("kind") or "").strip().lower()
    if raw:
        if raw in KINDS:
            return raw, None
        return "", f"kind must be one of: {', '.join(KINDS)}"
    tags = args.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            text = str(tag).strip()
            if text.startswith("kind:"):
                value = text[5:].strip().lower()
                if value in KINDS:
                    return value, None
                return "", f"kind must be one of: {', '.join(KINDS)}"
    return fallback, None


def _lifecycle_from_args(args: dict, fallback: str) -> tuple[str, str | None]:
    raw = str(args.get("lifecycle") or "").strip().lower()
    if not raw:
        return fallback, None
    if raw in LIFECYCLES:
        return raw, None
    return "", f"lifecycle must be one of: {', '.join(LIFECYCLES)}"


def _lifecycle_status(lifecycle: str) -> str:
    return STATUS_DEPRECATED if lifecycle == "deprecated" else STATUS_ACTIVE


def _resolve_source_file(root: Path, source: str) -> Path | None:
    text = source.strip()
    if not text or "://" in text or "\n" in text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.exists() and any(char.isspace() for char in text):
        return None
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _fingerprint_sources(root: Path, sources: list[str]) -> list[dict]:
    fingerprints: list[dict] = []
    for source in sources:
        path = _resolve_source_file(root, source)
        if path is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            fingerprints.append({"resource": source, "status": "missing"})
            continue
        if not path.is_file() or stat.st_size > _FINGERPRINT_MAX_BYTES:
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        fingerprints.append(
            {
                "resource": source,
                "sha256": digest,
                "mtime": int(stat.st_mtime),
                "size": stat.st_size,
            }
        )
    return fingerprints


def _source_status(root: Path, record: dict) -> tuple[str, dict[str, str]]:
    entries: dict[str, dict] = {}
    raw = record.get("source_fingerprints")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("resource"), str):
                entries[entry["resource"]] = entry
    details: dict[str, str] = {}
    for source in record.get("sources", []):
        path = _resolve_source_file(root, source)
        entry = entries.get(source)
        if path is None:
            details[source] = "unknown"
        elif not path.is_file():
            details[source] = "missing"
        elif not isinstance(entry, dict) or not entry.get("sha256"):
            details[source] = "unknown"
        else:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                details[source] = "unknown"
            else:
                details[source] = "ok" if digest == entry["sha256"] else "changed"
    if not details:
        return "unknown", details
    if "missing" in details.values():
        return "missing", details
    if "changed" in details.values():
        return "changed", details
    if "unknown" in details.values():
        return "unknown", details
    return "ok", details


def _merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load_config(plugin_root: Path, project_path: str | None = None) -> dict:
    config = json.loads(json.dumps(DEFAULTS))
    candidates = [plugin_root / ".pi" / "ltm" / "ltm.config.json"]
    if project_path:
        candidates.append(Path(project_path) / ".pi" / "ltm" / "ltm.config.json")
    for candidate in candidates:
        if candidate.is_file():
            try:
                _merge(config, json.loads(candidate.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    return config


def _resolve(plugin_root: Path, raw, cwd: str, config: dict) -> dict:
    info = project.resolve(raw, cwd)
    extra = [str(plugin_root.parent)]
    configured = config.get("allowedRoots")
    if isinstance(configured, list):
        extra.extend(str(item) for item in configured if item)
    project.assert_allowed(info["path"], project.allowed_roots(cwd, extra))
    return info


def _begin(plugin_root: Path, raw, cwd: str, config: dict) -> tuple[dict | None, dict | None]:
    if not config.get("enabled", True):
        return None, fail("DISABLED", "long-term memory is disabled by configuration")
    try:
        return _resolve(plugin_root, raw, cwd, config), None
    except LtmError as error:
        return None, fail(error.code, error.message, error.data)


def _lock_opts(config: dict) -> dict:
    lock = config.get("lock", {})
    return {
        "timeout_ms": int(lock.get("timeoutMs", 5000)),
        "stale_ms": int(lock.get("staleMs", 10000)),
        "retries": int(lock.get("retries", 8)),
    }


def _scope_root(info: dict, scope: str, config: dict) -> Path:
    raw = str(config.get("scopes", {}).get(scope, {}).get("root", ".pi/ltm"))
    if scope == "global" or raw.startswith("~"):
        return Path(os.path.expanduser(raw))
    return Path(info["path"]) / raw


def _ensure_bundle(plugin_root: Path, bundle: Path, config: dict) -> None:
    if okf_cli.init_bundle(plugin_root, bundle, config):
        sample = bundle / "concept.md"
        if sample.is_file():
            sample.unlink()


def _normalize_sources(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        items = [str(value)]
    return [item.strip() for item in items if item and item.strip()]


def _tags(value, fallback: list[str] | None, kind: str = "") -> list[str]:
    if value is None:
        cleaned = list(fallback) if fallback else ["memory"]
    else:
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = [str(item) for item in value]
        else:
            items = [str(value)]
        cleaned = [item.strip() for item in items if item and item.strip()]
    cleaned = [item for item in cleaned if not item.startswith("kind:")]
    if "memory" not in cleaned:
        cleaned.insert(0, "memory")
    if kind:
        cleaned.append(f"kind:{kind}")
    return cleaned


def _description(content: str) -> str:
    for line in content.splitlines():
        text = line.strip().lstrip("#-* ").strip()
        if text:
            return text[:120]
    return "project long-term memory"


def _new_id() -> str:
    return "mem_" + secrets.token_hex(4)


def _frontmatter(record: dict, base: dict | None = None) -> dict:
    data = dict(base or {})
    data["type"] = MEMORY_TYPE
    data["title"] = record["title"]
    data["description"] = record["description"]
    data["generated"] = {"by": GENERATED_BY, "at": record["generated_at"]}
    data["id"] = record["id"]
    data["key"] = record["key"]
    data["project"] = record["project"]
    data["projectId"] = record["project_id"]
    data["sources"] = [{"resource": str(item)} for item in record["sources"]]
    data["tags"] = record["tags"]
    data["status"] = _lifecycle_status(record.get("lifecycle", "active"))
    data["lifecycle"] = record.get("lifecycle", "active")
    data["sourceFingerprints"] = record.get("source_fingerprints", [])
    if record.get("supersedes"):
        data["supersedes"] = record["supersedes"]
    data["revision"] = record["revision"]
    return data


def _inspect(plugin_root: Path, file: Path, config: dict) -> dict | None:
    frontmatter = okf_cli.inspect_json(plugin_root, file, config)
    if frontmatter is None:
        return None
    try:
        body = concept.split_body(file.read_text(encoding="utf-8"))
    except OSError:
        body = ""
    generated = frontmatter.get("generated")
    generated = generated if isinstance(generated, dict) else {}
    key = str(frontmatter.get("key") or "")
    if not key:
        return None
    sources: list[str] = []
    raw_sources = frontmatter.get("sources")
    if isinstance(raw_sources, list):
        for entry in raw_sources:
            if isinstance(entry, dict):
                value = entry.get("resource")
                if isinstance(value, str) and value.strip():
                    sources.append(value.strip())
            elif isinstance(entry, str) and entry.strip():
                sources.append(entry.strip())
    tags = frontmatter.get("tags")
    tags = tags if isinstance(tags, list) else []
    status = str(frontmatter.get("status") or STATUS_ACTIVE)
    lifecycle = str(frontmatter.get("lifecycle") or "").strip().lower()
    if lifecycle not in LIFECYCLES:
        lifecycle = "deprecated" if status == STATUS_DEPRECATED else "active"
    fingerprints = frontmatter.get("sourceFingerprints")
    fingerprints = (
        [entry for entry in fingerprints if isinstance(entry, dict) and entry.get("resource")]
        if isinstance(fingerprints, list)
        else []
    )
    supersedes = frontmatter.get("supersedes")
    supersedes = (
        [entry for entry in supersedes if isinstance(entry, dict)]
        if isinstance(supersedes, list)
        else []
    )
    return {
        "id": str(frontmatter.get("id") or ""),
        "key": key,
        "title": str(frontmatter.get("title") or key),
        "description": str(frontmatter.get("description") or ""),
        "project": str(frontmatter.get("project") or ""),
        "project_id": str(frontmatter.get("projectId") or ""),
        "sources": sources,
        "tags": tags,
        "kind": _kind_from_tags(tags),
        "status": status,
        "lifecycle": lifecycle,
        "source_fingerprints": fingerprints,
        "supersedes": supersedes,
        "revision": int(frontmatter.get("revision") or 1),
        "generated_by": str(generated.get("by") or GENERATED_BY),
        "generated_at": str(generated.get("at") or ""),
        "content": concept.extract_content(body),
        "frontmatter": frontmatter,
    }


def _index_entries(bundle: Path) -> list[dict]:
    try:
        text = (bundle / "index.md").read_text(encoding="utf-8")
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        match = _INDEX_LINK.match(line)
        if not match:
            continue
        target = match.group(2).strip()
        if "://" in target or not target.endswith(".md"):
            continue
        rel = target.split("#", 1)[0]
        if not rel or "/" in rel or rel in {"index.md", "log.md"}:
            continue
        entries.append(
            {
                "title": match.group(1).strip(),
                "rel": rel,
                "description": (match.group(3) or "").strip(),
            }
        )
    return entries


def _load(
    plugin_root: Path,
    root: Path,
    config: dict,
    only: set[str] | None = None,
) -> list[dict]:
    bundle = root / "okf"
    cache_path = root / ".index" / "memory-index.json"
    entries = cache.sync(
        bundle,
        cache_path,
        lambda file: _inspect(plugin_root, file, config),
        only=only,
    )
    records = []
    for rel, entry in entries.items():
        record = entry.get("record")
        if not isinstance(record, dict):
            continue
        item = dict(record)
        item["path"] = str(bundle / rel)
        records.append(item)
    records.sort(key=lambda item: item.get("generated_at", ""), reverse=True)
    return records


def _post_write(plugin_root: Path, bundle: Path, config: dict, summary: str) -> list[str]:
    okf_cli.index_write(plugin_root, bundle, config)
    try:
        okf_cli.capture(plugin_root, bundle, config, summary)
    except okf_cli.OkfCliError as error:
        return [f"log.md sync failed: {str(error)[:500]}"]
    return []


def _gate(plugin_root: Path, bundle: Path, config: dict, target: Path, backup: bytes | None) -> dict | None:
    gate = config.get("gate", {})
    if not gate.get("validateOnWrite", True):
        return None
    result = okf_cli.validate(plugin_root, bundle, config)
    if result["rc"] != 1:
        return None
    if gate.get("rollbackOnInvalid", True):
        if backup is None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        else:
            concept.atomic_write_text(target, backup.decode("utf-8"))
    return fail("OKF_INVALID", "OKF validation failed; the change was rolled back", {"detail": result["payload"]})


def _candidates(records: list[dict], info: dict, rid: str, key: str) -> list[dict]:
    active = [
        item
        for item in records
        if item["status"] != STATUS_DEPRECATED and item.get("lifecycle") != "deprecated"
    ]
    if rid:
        return [item for item in active if item["id"] == rid]
    return [item for item in active if item["key"] == key and item["project_id"] == info["project_id"]]


def write(plugin_root: Path, cwd: str, args: dict, config: dict) -> dict:
    info, guard = _begin(plugin_root, args.get("project"), cwd, config)
    if guard is not None:
        return guard
    scope = str(args.get("scope") or "project")
    root = _scope_root(info, scope, config)
    bundle = root / "okf"
    key = str(args.get("key") or "").strip()
    content = str(args.get("content") or "").strip()
    sources = _normalize_sources(args.get("source"))
    if not key:
        return fail("INVALID_ARGS", "key is required")
    if not content:
        return fail("INVALID_ARGS", "content is required")
    if not sources:
        return fail("INVALID_ARGS", "source is required")
    kind, kind_error = _kind_from_args(args, "")
    if kind_error:
        return fail("INVALID_ARGS", kind_error)
    lifecycle, lifecycle_error = _lifecycle_from_args(args, "active")
    if lifecycle_error:
        return fail("INVALID_ARGS", lifecycle_error)
    fingerprints = _fingerprint_sources(Path(info["path"]), sources)
    scan_text = "\n".join(
        [content, str(args.get("title") or ""), str(args.get("description") or ""), *sources]
    )
    secret_reasons = _secret_reasons(scan_text)
    if secret_reasons:
        return fail(
            "SENSITIVE_CONTENT",
            "credentials, tokens, passwords and private keys are not stored in long-term memory",
            {"reasons": secret_reasons},
        )
    body = concept.build_body(content, sources)
    citation_errors = concept.citation_errors(body, sources)
    if citation_errors:
        return fail("CITATION_MISMATCH", "citation check failed", {"errors": citation_errors})

    with file_lock(root / ".lock", **_lock_opts(config)):
        _ensure_bundle(plugin_root, bundle, config)
        records = _load(plugin_root, root, config)
        same = [
            item
            for item in records
            if item["project_id"] == info["project_id"]
            and item["key"] == key
            and item["status"] != STATUS_DEPRECATED
            and item.get("lifecycle") != "deprecated"
        ]
        if same and not args.get("overwrite"):
            return fail(
                "CONFLICT",
                f"memory key already exists: {key}",
                {"id": same[0]["id"], "path": same[0]["path"], "hint": "call memory_update with this id"},
            )
        previous = same[0] if same else None
        kind, kind_error = _kind_from_args(args, previous.get("kind", "") if previous else "")
        if kind_error:
            return fail("INVALID_ARGS", kind_error)
        record = {
            "id": previous["id"] if previous else _new_id(),
            "key": key,
            "title": str(args.get("title") or (previous["title"] if previous else key)),
            "description": str(args.get("description") or _description(content)),
            "content": content,
            "sources": sources,
            "tags": _tags(args.get("tags"), previous["tags"] if previous else None, kind),
            "kind": kind,
            "lifecycle": lifecycle,
            "status": _lifecycle_status(lifecycle),
            "source_fingerprints": fingerprints,
            "supersedes": list(previous.get("supersedes") or []) if previous else [],
            "revision": (previous["revision"] + 1) if previous else 1,
            "project": info["path"],
            "project_id": info["project_id"],
            "generated_at": now_iso(),
        }
        target = bundle / concept.concept_relpath(record)
        backup = target.read_bytes() if target.exists() else None
        base = dict(previous["frontmatter"]) if previous else {}
        concept.write_document(target, _frontmatter(record, base), body)
        blocked = _gate(plugin_root, bundle, config, target, backup)
        if blocked is not None:
            _load(plugin_root, root, config)
            return blocked
        warnings = _post_write(plugin_root, bundle, config, f"write memory {key}")
        _load(plugin_root, root, config)

    payload = {
        "id": record["id"],
        "key": key,
        "path": str(target),
        "revision": record["revision"],
        "createdAt": record["generated_at"],
    }
    if warnings:
        payload["warnings"] = warnings
    return ok(payload)


def read(plugin_root: Path, cwd: str, args: dict, config: dict) -> dict:
    info, guard = _begin(plugin_root, args.get("project"), cwd, config)
    if guard is not None:
        return guard
    scope = str(args.get("scope") or "project")
    key = str(args.get("key") or "").strip()
    query = str(args.get("query") or "").strip().lower()
    limit = int(args.get("limit") or 10)
    include_expired = bool(args.get("includeExpired"))
    verify_sources = args.get("verifySources") is not False
    roots: list[Path] = []
    if scope in ("project", "all"):
        roots.append(_scope_root(info, "project", config))
    if scope in ("global", "all"):
        roots.append(_scope_root(info, "global", config))

    items: list[dict] = []

    def collect(records: list[dict]) -> list[dict]:
        collected = []
        for record in records:
            if scope != "global" and record["project_id"] != info["project_id"]:
                continue
            if record.get("lifecycle") == "expired" and not include_expired:
                continue
            if key and record["key"] != key:
                continue
            if query:
                haystack = " ".join(
                    [
                        record["key"],
                        record["title"],
                        record["content"],
                        " ".join(str(item) for item in record["sources"]),
                    ]
                ).lower()
                if query not in haystack:
                    continue
            item = {name: value for name, value in record.items() if name != "frontmatter"}
            item["exists"] = Path(item["path"]).is_file()
            collected.append(item)
        return collected

    for root in roots:
        bundle = root / "okf"
        if not bundle.is_dir():
            continue
        records = None
        used_index = False
        if key or query:
            entries = _index_entries(bundle)
            if key:
                slug = concept.slugify(key)
                candidates = {
                    entry["rel"]
                    for entry in entries
                    if Path(entry["rel"]).name == f"{slug}.md"
                    or Path(entry["rel"]).name.startswith(f"{slug}-")
                }
            else:
                candidates = {
                    entry["rel"]
                    for entry in entries
                    if query in f"{entry['title']} {entry['description']}".lower()
                }
            if candidates:
                with file_lock(root / ".lock", **_lock_opts(config)):
                    records = _load(plugin_root, root, config, only=candidates)
                used_index = True
        if records is None:
            with file_lock(root / ".lock", **_lock_opts(config)):
                records = _load(plugin_root, root, config)
        root_items = collect(records)
        if used_index and not root_items:
            with file_lock(root / ".lock", **_lock_opts(config)):
                records = _load(plugin_root, root, config)
            root_items = collect(records)
        items.extend(root_items)

    items = items[:limit]
    if verify_sources:
        for item in items:
            status, details = _source_status(Path(info["path"]), item)
            item["source_status"] = status
            item["source_details"] = details
            item["stale"] = status in ("changed", "missing")
    if not items:
        return fail(
            "NOT_FOUND",
            "no matching memory record in the long-term memory store",
            {"scope": scope, "key": key, "query": query},
        )
    return ok({"count": len(items), "items": items, "project": info["path"]})


def update(plugin_root: Path, cwd: str, args: dict, config: dict) -> dict:
    info, guard = _begin(plugin_root, args.get("project"), cwd, config)
    if guard is not None:
        return guard
    scope = str(args.get("scope") or "project")
    root = _scope_root(info, scope, config)
    bundle = root / "okf"
    rid = str(args.get("id") or "").strip()
    key = str(args.get("key") or "").strip()
    if not rid and not key:
        return fail("INVALID_ARGS", "id or key is required")

    with file_lock(root / ".lock", **_lock_opts(config)):
        records = _load(plugin_root, root, config)
        candidates = _candidates(records, info, rid, key)
        if not candidates:
            return fail("NOT_FOUND", "no matching memory record", {"id": rid, "key": key})
        if len(candidates) > 1:
            return fail(
                "AMBIGUOUS",
                "multiple records match; retry with an explicit id",
                {"ids": [item["id"] for item in candidates]},
            )
        current = candidates[0]
        record = dict(current)
        kind, kind_error = _kind_from_args(args, current.get("kind", ""))
        if kind_error:
            return fail("INVALID_ARGS", kind_error)
        lifecycle, lifecycle_error = _lifecycle_from_args(args, current.get("lifecycle", "active"))
        if lifecycle_error:
            return fail("INVALID_ARGS", lifecycle_error)
        if args.get("content") is not None:
            record["content"] = str(args["content"]).strip()
        if args.get("source") is not None:
            record["sources"] = _normalize_sources(args["source"])
        if args.get("title") is not None:
            record["title"] = str(args["title"]).strip() or record["key"]
        if args.get("tags") is not None or kind != current.get("kind", ""):
            record["tags"] = _tags(args.get("tags"), record["tags"], kind)
        record["kind"] = kind
        record["lifecycle"] = lifecycle
        record["status"] = _lifecycle_status(lifecycle)
        record["source_fingerprints"] = _fingerprint_sources(Path(info["path"]), record["sources"])
        record["revision"] = current["revision"] + 1
        record["generated_at"] = now_iso()
        record["description"] = str(args.get("description") or _description(record["content"]))

        scan_text = "\n".join(
            [record["content"], record["title"], record["description"], *record["sources"]]
        )
        secret_reasons = _secret_reasons(scan_text)
        if secret_reasons:
            return fail(
                "SENSITIVE_CONTENT",
                "credentials, tokens, passwords and private keys are not stored in long-term memory",
                {"reasons": secret_reasons},
            )

        body = concept.build_body(record["content"], record["sources"])
        citation_errors = concept.citation_errors(body, record["sources"])
        if citation_errors:
            return fail("CITATION_MISMATCH", "citation check failed", {"errors": citation_errors})

        current_status, current_details = _source_status(Path(info["path"]), current)
        reason = str(args.get("reason") or "").strip()
        if current_status in ("changed", "missing") and (not args.get("confirm") or not reason):
            return fail(
                "CONFIRM_REQUIRED",
                "source changed or missing; re-read the source and retry with confirm=true and a reason",
                {"source_status": current_status, "source_details": current_details},
            )
        if reason:
            record["supersedes"] = list(current.get("supersedes") or []) + [
                {
                    "revision": current["revision"],
                    "at": current["generated_at"],
                    "reason": reason,
                }
            ]

        target = Path(current["path"])
        backup = target.read_bytes()
        concept.write_document(
            target,
            _frontmatter(record, current["frontmatter"]),
            body,
        )
        blocked = _gate(plugin_root, bundle, config, target, backup)
        if blocked is not None:
            _load(plugin_root, root, config)
            return blocked
        warnings = _post_write(plugin_root, bundle, config, f"update memory {record['key']}")
        _load(plugin_root, root, config)

    payload = {
        "id": record["id"],
        "key": record["key"],
        "path": str(target),
        "revision": record["revision"],
        "updatedAt": record["generated_at"],
    }
    if warnings:
        payload["warnings"] = warnings
    return ok(payload)


def delete(plugin_root: Path, cwd: str, args: dict, config: dict) -> dict:
    info, guard = _begin(plugin_root, args.get("project"), cwd, config)
    if guard is not None:
        return guard
    scope = str(args.get("scope") or "project")
    root = _scope_root(info, scope, config)
    bundle = root / "okf"
    rid = str(args.get("id") or "").strip()
    key = str(args.get("key") or "").strip()
    if not rid and not key:
        return fail("INVALID_ARGS", "id or key is required")

    with file_lock(root / ".lock", **_lock_opts(config)):
        records = _load(plugin_root, root, config)
        candidates = _candidates(records, info, rid, key)
        if not candidates:
            return fail("NOT_FOUND", "no matching memory record", {"id": rid, "key": key})
        if len(candidates) > 1:
            return fail(
                "AMBIGUOUS",
                "multiple records match; retry with an explicit id",
                {"ids": [item["id"] for item in candidates]},
            )
        current = candidates[0]
        target = Path(current["path"])

        if args.get("hard"):
            target.unlink(missing_ok=True)
            result_path = str(target)
            mode = "hard"
        else:
            record = dict(current)
            record["status"] = STATUS_DEPRECATED
            record["lifecycle"] = "deprecated"
            record["generated_at"] = now_iso()
            concept.write_document(
                target,
                _frontmatter(record, current["frontmatter"]),
                concept.build_body(record["content"], record["sources"]),
            )
            trash = bundle / "trash"
            trash.mkdir(parents=True, exist_ok=True)
            destination = trash / f"{target.name}.orig"
            os.replace(target, destination)
            result_path = str(destination)
            mode = "trash"

        warnings = _post_write(plugin_root, bundle, config, f"delete memory {current['key']}")
        _load(plugin_root, root, config)

    payload = {"id": current["id"], "key": current["key"], "path": result_path, "mode": mode}
    if warnings:
        payload["warnings"] = warnings
    return ok(payload)


def warm(plugin_root: Path, cwd: str, args: dict, config: dict) -> dict:
    info = _resolve(plugin_root, args.get("project"), cwd, config)
    session = config.get("session", {})
    max_keys = max(0, int(session.get("maxKeysPerScope", 20)))
    summary = {}
    for scope in ("project", "global"):
        root = _scope_root(info, scope, config)
        bundle = root / "okf"
        if not bundle.is_dir():
            summary[scope] = {
                "records": 0,
                "bundle": str(bundle),
                "exists": False,
                "keys": [],
                "totalKeys": 0,
            }
            continue
        with file_lock(root / ".lock", **_lock_opts(config)):
            records = _load(plugin_root, root, config)
        active = [item for item in records if item.get("lifecycle") != "expired"]
        keys = [item["key"] for item in active]
        summary[scope] = {
            "records": len(records),
            "activeRecords": len(active),
            "expiredRecords": len(records) - len(active),
            "bundle": str(bundle),
            "exists": True,
            "keys": keys[:max_keys],
            "totalKeys": len(keys),
        }
    return ok({"project": info["path"], "scopes": summary})


def status(plugin_root: Path, cwd: str, args: dict, config: dict) -> dict:
    info = _resolve(plugin_root, args.get("project"), cwd, config)
    root = _scope_root(info, "project", config)
    records = _load(plugin_root, root, config) if (root / "okf").is_dir() else []
    return ok(
        {
            "enabled": bool(config.get("enabled", True)),
            "project": info["path"],
            "projectId": info["project_id"],
            "bundle": str(root / "okf"),
            "index": str(root / ".index" / "memory-index.json"),
            "lock": str(root / ".lock"),
            "records": len(records),
            "keys": [item["key"] for item in records],
        }
    )
