"""Write and sync command handlers for RouteCraft memory."""
from __future__ import annotations

from .common import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403
from .learning import *  # noqa: F401,F403
from .git_sync import *  # noqa: F401,F403

def cmd_learn(args: argparse.Namespace) -> int:
    config = load_config()
    store = resolve_store(args.store, config)
    ensure_store_layout(store)
    ensure_external_write_store(store)
    device_id = resolve_device_id(config)
    packet = merge_cli_packet(args, load_json_packet(args.input))
    reinforce_ids = normalize_string_list(packet.get("reinforce_candidates"), "reinforce_candidates")
    if reinforce_ids and str(packet.get("kind", "")).strip().lower() != "case":
        raise RouteCraftError("reinforce_candidates may only be used when learning a verified case")

    with StoreLock(store, "learn"):
        reinforce_records = [find_record(store, candidate_id, "candidate") for candidate_id in reinforce_ids]
        original_contents = (
            {record.path: record.path.read_text(encoding="utf-8") for record in reinforce_records}
            if not args.dry_run
            else {}
        )
        created_paths: list[Path] = []
        try:
            created, path = create_learning_record(
                store,
                packet,
                device_id=device_id,
                body_file=args.body_file,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                created_paths.append(path)
            created_records = [created.record_id]
            updated_candidates: list[str] = []
            eligible: list[str] = []
            valid_case_ids = {record.record_id for record in load_records(store) if record.kind == "case"}
            if created.kind == "case":
                valid_case_ids.add(created.record_id)
                nested = packet.get("candidate")
                if nested is not None:
                    if not isinstance(nested, dict):
                        raise RouteCraftError("candidate in learning packet must be an object")
                    nested_packet = dict(nested)
                    nested_packet["kind"] = "candidate"
                    nested_evidence = normalize_string_list(nested_packet.get("evidence"), "candidate.evidence")
                    if created.record_id not in nested_evidence:
                        nested_evidence.append(created.record_id)
                    nested_packet["evidence"] = nested_evidence
                    nested_packet.setdefault("observations", 1)
                    candidate, candidate_path = create_learning_record(
                        store, nested_packet, device_id=device_id, dry_run=args.dry_run
                    )
                    if not args.dry_run:
                        created_paths.append(candidate_path)
                    created_records.append(candidate.record_id)
                for candidate_record in reinforce_records:
                    reinforced = reinforce_candidate(
                        candidate_record,
                        created.record_id,
                        confidence=parse_float_value(
                            packet["candidate_confidence"], "candidate confidence", minimum=0, maximum=1
                        )
                        if packet.get("candidate_confidence") is not None
                        else None,
                        dry_run=args.dry_run,
                    )
                    updated_candidates.append(reinforced.record_id)
                    if candidate_eligible(reinforced, valid_case_ids):
                        eligible.append(reinforced.record_id)
            if not args.dry_run:
                build_index(store, write=True, markdown=args.markdown_index)
        except Exception as exc:
            if not args.dry_run:
                rollback_memory_mutations(created_paths, original_contents)
                with contextlib.suppress(Exception):
                    build_index(store, write=True, markdown=args.markdown_index)
            if isinstance(exc, OSError):
                raise RouteCraftError(f"Could not persist learning records: {exc}") from exc
            raise
    sync_result = None if args.dry_run else maybe_sync_after_write(store, config, args, device_id)
    output = {
        "store": str(store),
        "created": created_records,
        "primary_path": str(path),
        "updated_candidates": updated_candidates,
        "eligible_for_promotion": eligible,
        "dry_run": args.dry_run,
        "sync": sync_result,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    config = load_config()
    store = resolve_store(args.store, config)
    ensure_store_layout(store)
    ensure_external_write_store(store)
    device_id = resolve_device_id(config)
    packet = merge_cli_packet(args, load_json_packet(args.input))
    candidate_id = str(packet.get("candidate_id", "")).strip()
    if not candidate_id:
        raise RouteCraftError("promote requires --candidate-id or candidate_id in the input packet")
    with StoreLock(store, "promote"):
        candidate = find_record(store, candidate_id, "candidate")
        if str(candidate.metadata.get("status")) == "promoted":
            raise RouteCraftError(
                f"Candidate {candidate_id} is already promoted to {candidate.metadata.get('promoted_to')}"
            )
        candidate_evidence = normalize_string_list(candidate.metadata.get("evidence"), "candidate.evidence")
        packet_evidence = normalize_string_list(packet.get("evidence"), "evidence")
        evidence = list(dict.fromkeys(candidate_evidence + packet_evidence))
        observations = max(
            parse_int_value(candidate.metadata.get("observations", 1), "candidate observations", minimum=1),
            parse_int_value(packet.get("observations", 1), "observations", minimum=1, maximum=1_000_000),
        )
        min_observations = parse_int_value(args.min_observations, "min observations", minimum=1, maximum=1_000_000)
        min_evidence = parse_int_value(args.min_evidence, "min evidence", minimum=1, maximum=1_000_000)
        valid_case_ids = {record.record_id for record in load_records(store) if record.kind == "case"}
        verified_case_evidence = {item for item in evidence if item in valid_case_ids}
        authoritative_path = bool(args.authoritative and args.human_approved)
        normal_path = observations >= min_observations and len(verified_case_evidence) >= min_evidence
        if not normal_path and not (authoritative_path and evidence):
            raise RouteCraftError(
                "Promotion gate not met. A normal promotion requires at least "
                f"{min_observations} observations and {min_evidence} captured Case records as unique evidence. "
                "The exceptional path requires both --authoritative and --human-approved plus evidence."
            )
        title = str(packet.get("title") or candidate.title).strip()
        decision = str(packet.get("decision", "")).strip()
        if not decision:
            raise RouteCraftError("A promoted rule requires a decision statement")
        sections: dict[str, str] = {
            "Decision": decision,
            "When to apply": str(packet.get("when_to_apply", "")).strip(),
            "When not to apply": str(packet.get("when_not_to_apply", "")).strip(),
            "Rationale": str(packet.get("rationale", "")).strip(),
            "Verification": str(packet.get("verification", "")).strip(),
            "Evidence": "\n".join(f"- {item}" for item in evidence),
        }
        body = render_sections("rule", title, sections)
        now = utc_now()
        candidate_confidence = parse_float_value(
            candidate.metadata.get("confidence", 0.5), "candidate confidence", minimum=0, maximum=1
        )
        confidence_default = max(0.9 if authoritative_path else 0.8, candidate_confidence)
        confidence = parse_float_value(
            packet.get("confidence", confidence_default), "confidence", minimum=0, maximum=1
        )
        metadata: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "id": make_record_id("rule", device_id),
            "kind": "rule",
            "title": title,
            "status": "validated",
            "confidence": round(confidence, 3),
            "observations": observations,
            "tags": normalize_string_list(packet.get("tags") or candidate.metadata.get("tags"), "tags"),
            "scope": normalize_string_list(packet.get("scope") or candidate.metadata.get("scope"), "scope"),
            "created_at": now,
            "updated_at": now,
            "last_verified": now,
            "device_id": device_id,
            "evidence": evidence,
            "source_candidate": candidate_id,
        }
        created_paths: list[Path] = []
        original_contents = (
            {candidate.path: candidate.path.read_text(encoding="utf-8")} if not args.dry_run else {}
        )
        try:
            rule_path = write_record(store, "rule", metadata, body, dry_run=args.dry_run)
            if not args.dry_run:
                created_paths.append(rule_path)
            candidate_meta = dict(candidate.metadata)
            candidate_meta["status"] = "promoted"
            candidate_meta["promoted_to"] = metadata["id"]
            candidate_meta["updated_at"] = now
            update_record(candidate, candidate_meta, dry_run=args.dry_run)
            if not args.dry_run:
                build_index(store, write=True, markdown=args.markdown_index)
        except Exception as exc:
            if not args.dry_run:
                rollback_memory_mutations(created_paths, original_contents)
                with contextlib.suppress(Exception):
                    build_index(store, write=True, markdown=args.markdown_index)
            if isinstance(exc, OSError):
                raise RouteCraftError(f"Could not persist promoted rule: {exc}") from exc
            raise
    sync_result = None if args.dry_run else maybe_sync_after_write(store, config, args, device_id)
    print(
        json.dumps(
            {
                "store": str(store),
                "candidate": candidate_id,
                "rule": metadata["id"],
                "rule_path": str(rule_path),
                "promotion_path": "authoritative-human-approved" if authoritative_path and not normal_path else "repeated-observation",
                "dry_run": args.dry_run,
                "sync": sync_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config()
    store = resolve_store(args.store, config)
    ensure_store_layout(store)
    ensure_external_write_store(store)
    device_id = resolve_device_id(config)
    remote = args.remote or str(config.get("remote", DEFAULT_REMOTE))
    branch = args.branch or str(config.get("branch", DEFAULT_BRANCH))
    retries = parse_int_value(args.retries, "retries", minimum=0, maximum=10)
    with StoreLock(store, "sync"):
        build_index(store, write=True)
        result = sync_store(
            store,
            device_id=device_id,
            remote=remote,
            branch=branch,
            mode=args.mode,
            message=args.message,
            retries=retries,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
