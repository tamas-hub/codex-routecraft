"""Read-oriented command handlers for RouteCraft memory."""
from __future__ import annotations

from .common import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403
from .learning import *  # noqa: F401,F403
from .git_sync import *  # noqa: F401,F403

def cmd_init(args: argparse.Namespace) -> int:
    if args.clone and (args.git_init or args.remote or args.adopt_existing):
        raise RouteCraftError("--clone cannot be combined with --git-init, --remote, or --adopt-existing")
    require_git_if_requested = bool(args.git_init or args.remote or args.clone)
    if require_git_if_requested:
        require_git()
    store = Path(args.store).expanduser().resolve()
    ensure_external_write_store(store)
    branch = validate_branch_name(store.parent if not store.exists() else store, args.branch or DEFAULT_BRANCH)
    remote_name = validate_remote_name(args.remote_name or DEFAULT_REMOTE)
    if args.remote:
        validate_remote_location(args.remote)
    if args.clone:
        validate_remote_location(args.clone)
        if store.exists() and any(store.iterdir()):
            raise RouteCraftError(f"Clone destination is not empty: {store}")
        store.parent.mkdir(parents=True, exist_ok=True)
        clone_args = ["clone"]
        if branch:
            clone_args.extend(["--branch", branch])
        clone_args.extend(["--", args.clone, str(store)])
        process = subprocess.run(["git", *clone_args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if process.returncode != 0:
            raise GitCommandError(clone_args, process.returncode, process.stderr)
        if not store_sentinel(store).is_file():
            raise RouteCraftError(
                f"Cloned repository is not a RouteCraft memory store (missing .routecraft-store.json): {store}"
            )
        ensure_store_layout(store, create=True)
    else:
        if store.exists() and any(store.iterdir()) and not store_sentinel(store).is_file() and not args.adopt_existing:
            raise RouteCraftError(
                f"Refusing to initialize a non-empty directory without --adopt-existing: {store}"
            )
        ensure_store_layout(store, create=True, name=args.name)
        if args.git_init or args.remote:
            if not (store / ".git").exists():
                init_result = subprocess.run(
                    ["git", "init", "-b", branch, str(store)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if init_result.returncode != 0:
                    init_result = subprocess.run(
                        ["git", "init", str(store)],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    if init_result.returncode != 0:
                        raise GitCommandError(["init", str(store)], init_result.returncode, init_result.stderr)
                    run_git(store, ["checkout", "-b", branch])
            if args.remote:
                if git_remote_exists(store, remote_name):
                    run_git(store, ["remote", "set-url", remote_name, args.remote])
                else:
                    run_git(store, ["remote", "add", remote_name, args.remote])
    build_index(store, write=True, markdown=args.markdown_index)
    config_path: Path | None = None
    if args.configure:
        existing = load_config()
        config = dict(existing)
        config.update(
            {
                "schema_version": 1,
                "store": str(store),
                "device_id": str(existing.get("device_id") or generate_device_id()),
                "auto_sync": args.auto_sync,
                "remote": remote_name,
                "branch": branch,
            }
        )
        config_path = save_config(config)
    output = {"store": str(store), "configured": str(config_path) if config_path else None, "git": is_git_repository(store)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    existing = load_config()
    config = dict(existing)
    if args.store:
        store = Path(args.store).expanduser().resolve()
        ensure_store_layout(store)
        ensure_external_write_store(store)
        config["store"] = str(store)
    elif "store" not in config:
        raise RouteCraftError("configure requires --store the first time")
    if args.device_id:
        clean = SAFE_DEVICE_RE.sub("", args.device_id).lower()[:12]
        if not clean:
            raise RouteCraftError("device id must contain letters or numbers")
        config["device_id"] = clean
    else:
        config.setdefault("device_id", generate_device_id())
    if args.auto_sync is not None:
        config["auto_sync"] = args.auto_sync
    else:
        config.setdefault("auto_sync", "off")
    if args.remote:
        config["remote"] = validate_remote_name(args.remote)
    else:
        config.setdefault("remote", DEFAULT_REMOTE)
    if args.branch:
        config["branch"] = validate_branch_name(Path(str(config["store"])), args.branch)
    else:
        config.setdefault("branch", DEFAULT_BRANCH)
    config["schema_version"] = 1
    path = save_config(config)
    print(json.dumps({"config": str(path), **config}, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    store = resolve_store(args.store, config)
    ensure_store_layout(store)
    records = load_records(store)
    counts = {kind: sum(1 for record in records if record.kind == kind) for kind in KIND_TO_DIR}
    valid_case_ids = {record.record_id for record in records if record.kind == "case"}
    eligible = [record.record_id for record in records if candidate_eligible(record, valid_case_ids)]
    payload: dict[str, Any] = {
        "store": str(store),
        "config": str(default_config_path()),
        "device_id": resolve_device_id(config),
        "counts": counts,
        "eligible_candidates": eligible,
        "auto_sync": config.get("auto_sync", "off"),
        "git": {"is_repository": is_git_repository(store)},
    }
    if is_git_repository(store):
        remote = str(config.get("remote", DEFAULT_REMOTE))
        payload["git"].update(
            {
                "branch": git_branch(store, str(config.get("branch", DEFAULT_BRANCH))),
                "remote": remote,
                "remote_configured": git_remote_exists(store, remote),
                "dirty": working_tree_dirty(store),
                "conflicts": git_conflicts(store),
                "root": str(git_root(store)) if git_root(store) else None,
                "dedicated_root": git_root(store) == store.resolve(),
            }
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"RouteCraft memory store: {payload['store']}")
        print(f"Device: {payload['device_id']}")
        print("Records: " + ", ".join(f"{kind}={count}" for kind, count in counts.items()))
        print(f"Eligible candidates: {len(eligible)}")
        print(f"Auto sync: {payload['auto_sync']}")
        print(f"Git repository: {payload['git']['is_repository']}")
        if payload["git"]["is_repository"]:
            print(
                f"Git: branch={payload['git']['branch']} remote={payload['git']['remote']} "
                f"dirty={payload['git']['dirty']} conflicts={len(payload['git']['conflicts'])}"
            )
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    store = resolve_store(args.store)
    with StoreLock(store, "reindex"):
        payload = build_index(store, write=True, markdown=args.markdown)
    print(json.dumps({"store": str(store), "records": payload["record_count"], "fingerprint": payload["fingerprint"]}, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    store = resolve_store(args.store)
    ensure_store_layout(store)
    records = load_records(store)
    by_id = {record.record_id: record for record in records}
    promoted_targets = {record.record_id for record in records if record.kind == "rule"}
    errors: list[str] = []
    for record in records:
        status = str(record.metadata.get("status", ""))
        if record.kind == "candidate" and status not in {"candidate", "promoted"}:
            errors.append(f"{record.record_id}: unsupported candidate status {status!r}")
        if record.kind == "case" and status != "closed":
            errors.append(f"{record.record_id}: case status must be 'closed'")
        if record.kind == "rule" and status != "validated":
            errors.append(f"{record.record_id}: rule status must be 'validated'")
        if record.kind == "candidate" and status == "promoted":
            target = str(record.metadata.get("promoted_to", ""))
            if not target or target not in promoted_targets:
                errors.append(f"{record.record_id}: promoted candidate points to missing rule {target!r}")
        source_candidate = str(record.metadata.get("source_candidate", "")).strip()
        if source_candidate:
            source = by_id.get(source_candidate)
            if source is None or source.kind != "candidate":
                errors.append(f"{record.record_id}: source_candidate points to missing candidate {source_candidate!r}")
        for related_rule in normalize_string_list(record.metadata.get("related_rules"), "related_rules"):
            related = by_id.get(related_rule)
            if related is None or related.kind != "rule":
                errors.append(f"{record.record_id}: related_rules points to missing rule {related_rule!r}")
        for evidence_ref in normalize_string_list(record.metadata.get("evidence"), "evidence"):
            if VALID_ID_RE.fullmatch(evidence_ref) and evidence_ref not in by_id:
                errors.append(f"{record.record_id}: evidence points to missing record {evidence_ref!r}")
    if errors:
        raise RouteCraftError("Memory-store validation failed:\n- " + "\n- ".join(errors))
    print(f"RouteCraft memory validation OK: {len(records)} records in {store}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    config = load_config()
    store = resolve_store(args.store, config)
    ensure_store_layout(store)
    if not args.query and not args.tag:
        raise RouteCraftError("recall requires --query and/or at least one --tag")
    limit = parse_int_value(args.limit, "limit", minimum=1, maximum=50)
    budget = parse_int_value(args.budget, "budget", minimum=500, maximum=200_000)
    device_id = resolve_device_id(config)
    sync_result = maybe_sync_before_recall(store, config, args, device_id)
    if store_is_writable(store):
        with StoreLock(store, "recall-index"):
            result = recall_records(store, args.query or "", args.tag or [], limit=limit, budget=budget)
    else:
        result = recall_records(store, args.query or "", args.tag or [], limit=limit, budget=budget)
    if sync_result:
        result["sync"] = sync_result
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_recall_markdown(result, paths_only=args.paths_only), end="")
    return 0
