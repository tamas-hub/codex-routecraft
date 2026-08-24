"""Command-line interface for RouteCraft Memory Local."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import IMPORTANCE_LEVELS, MEMORY_TYPES, VERSION
from .errors import RouteCraftLocalError
from .git_tools import inspect_git, rule_based_session_summary
from .loop_bridge import configure as configure_loop
from .loop_bridge import status as loop_status
from .packs import build_context_pack, build_handoff_pack
from .service import RouteCraftService


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _emit(data: Any, *, json_mode: bool, human: str | None = None) -> None:
    if json_mode:
        print(_json_text({"ok": True, "data": data}))
    elif human is not None:
        print(human)
    elif isinstance(data, str):
        print(data)
    else:
        print(_json_text(data))


def _read_body(body: str | None, input_file: str | None) -> str:
    if body is not None and input_file is not None:
        raise RouteCraftLocalError("--body と --input-file は同時に指定できません。")
    if input_file:
        if input_file == "-":
            return sys.stdin.read()
        try:
            return Path(input_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RouteCraftLocalError(f"入力ファイルを読めません: {exc}") from exc
    if body == "-":
        return sys.stdin.read()
    if body is not None:
        return body
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _csv(values: Sequence[str] | None) -> list[str]:
    output: list[str] = []
    for value in values or ():
        output.extend(item.strip() for item in value.split(",") if item.strip())
    return list(dict.fromkeys(output))


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="機械可読な JSON で出力")


def _add_project_ref(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, help="プロジェクト ID または完全な名前")


def _add_search_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", action="append", choices=MEMORY_TYPES, dest="types")
    parser.add_argument("--tag", action="append", dest="tags")
    parser.add_argument("--importance", action="append", choices=IMPORTANCE_LEVELS)
    parser.add_argument("--from", dest="created_from", help="作成日時の下限 (ISO-8601)")
    parser.add_argument("--to", dest="created_to", help="作成日時の上限 (ISO-8601)")
    parser.add_argument("--file", dest="filename", help="関連ファイルの部分一致")
    parser.add_argument("--commit", help="関連コミットの部分一致")
    parser.add_argument("--active", choices=("yes", "no", "any"), default="yes")
    parser.add_argument("--verified", choices=("yes", "no", "any"), default="any")
    parser.add_argument("--limit", type=int, default=50)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="routecraft",
        description="昨日のAI開発の続きを、今日のAIへ正確に引き継ぐローカル記憶ツール",
    )
    parser.add_argument("--version", action="version", version=f"routecraft {VERSION}")
    parser.add_argument("--data-dir", help="DB・backup・export の保存先（既定: ~/.routecraft-memory-local）")
    parser.add_argument("--json", dest="global_json", action="store_true", help="機械可読な JSON で出力")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="ローカルDBを初期化")
    _add_json_flag(init)

    project = commands.add_parser("project", help="プロジェクト管理")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_add = project_commands.add_parser("add", help="プロジェクトを登録")
    project_add.add_argument("--name")
    project_add.add_argument("--repo", "--from-repo", dest="repo_path")
    project_add.add_argument("--remote-url", default="")
    project_add.add_argument("--agent", action="append")
    project_add.add_argument("--language", action="append")
    project_add.add_argument("--tag", action="append")
    project_add.add_argument("--description", default="")
    project_add.add_argument("--objective", default="")
    _add_json_flag(project_add)

    project_list = project_commands.add_parser("list", help="プロジェクト一覧")
    project_list.add_argument("--include-archived", action="store_true")
    _add_json_flag(project_list)

    project_show = project_commands.add_parser("show", help="プロジェクト詳細")
    _add_project_ref(project_show)
    _add_json_flag(project_show)

    project_rename = project_commands.add_parser("rename", help="名前を変更")
    _add_project_ref(project_rename)
    project_rename.add_argument("--name", required=True)
    _add_json_flag(project_rename)

    project_edit = project_commands.add_parser("edit", help="プロジェクト情報を編集")
    _add_project_ref(project_edit)
    project_edit.add_argument("--repo")
    project_edit.add_argument("--remote-url")
    project_edit.add_argument("--agent", action="append")
    project_edit.add_argument("--language", action="append")
    project_edit.add_argument("--tag", action="append")
    project_edit.add_argument("--description")
    project_edit.add_argument("--objective")
    _add_json_flag(project_edit)

    project_archive = project_commands.add_parser("archive", help="アーカイブ状態を変更")
    _add_project_ref(project_archive)
    project_archive.add_argument("--undo", action="store_true", help="アーカイブを解除")
    _add_json_flag(project_archive)

    project_delete = project_commands.add_parser("delete", help="確認付きでプロジェクトを削除")
    _add_project_ref(project_delete)
    project_delete.add_argument("--confirm", required=True, help="対象のプロジェクト ID を完全入力")
    _add_json_flag(project_delete)

    project_backup = project_commands.add_parser("backup", help="プロジェクト持ち運びパッケージを作成")
    _add_project_ref(project_backup)
    project_backup.add_argument("--output", required=True)
    project_backup.add_argument("--folder", action="store_true", help="ZIPではなくフォルダを作成")
    _add_json_flag(project_backup)

    project_restore = project_commands.add_parser("restore", help="プロジェクトパッケージを取り込み")
    project_restore.add_argument("--input", required=True)
    project_restore.add_argument("--conflict", choices=("detect", "skip"), default="detect")
    _add_json_flag(project_restore)

    memory = commands.add_parser("memory", help="構造化メモリ管理")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_add = memory_commands.add_parser("add", help="メモリを登録")
    _add_project_ref(memory_add)
    memory_add.add_argument("--type", required=True, choices=MEMORY_TYPES, dest="memory_type")
    memory_add.add_argument("--title", required=True)
    memory_add.add_argument("--body")
    memory_add.add_argument("--input-file")
    memory_add.add_argument("--importance", choices=IMPORTANCE_LEVELS, default="medium")
    memory_add.add_argument("--tag", action="append")
    memory_add.add_argument("--source", default="cli")
    memory_add.add_argument("--file", action="append", dest="related_files")
    memory_add.add_argument("--commit", action="append", dest="related_commits")
    memory_add.add_argument("--verified", action="store_true")
    _add_json_flag(memory_add)

    memory_list = memory_commands.add_parser("list", help="メモリ一覧")
    memory_list.add_argument("--project")
    memory_list.add_argument("--include-inactive", action="store_true")
    memory_list.add_argument("--type", action="append", choices=MEMORY_TYPES, dest="types")
    memory_list.add_argument("--importance", action="append", choices=IMPORTANCE_LEVELS)
    memory_list.add_argument("--limit", type=int, default=100)
    memory_list.add_argument("--offset", type=int, default=0)
    _add_json_flag(memory_list)

    memory_show = memory_commands.add_parser("show", help="メモリ詳細")
    memory_show.add_argument("--id", required=True)
    _add_json_flag(memory_show)

    memory_edit = memory_commands.add_parser("edit", help="メモリを編集")
    memory_edit.add_argument("--id", required=True)
    memory_edit.add_argument("--type", choices=MEMORY_TYPES, dest="memory_type")
    memory_edit.add_argument("--title")
    memory_edit.add_argument("--body")
    memory_edit.add_argument("--input-file")
    memory_edit.add_argument("--importance", choices=IMPORTANCE_LEVELS)
    memory_edit.add_argument("--tag", action="append")
    memory_edit.add_argument("--file", action="append", dest="related_files")
    memory_edit.add_argument("--commit", action="append", dest="related_commits")
    memory_edit.add_argument("--active", choices=("yes", "no"))
    memory_edit.add_argument("--verified", choices=("yes", "no"))
    _add_json_flag(memory_edit)

    memory_delete = memory_commands.add_parser("delete", help="確認付きでメモリを削除")
    memory_delete.add_argument("--id", required=True)
    memory_delete.add_argument("--confirm", required=True, help="対象のメモリ ID を完全入力")
    _add_json_flag(memory_delete)

    memory_search = memory_commands.add_parser("search", help="ローカル全文検索")
    memory_search.add_argument("query", nargs="?", default="")
    memory_search.add_argument("--project")
    _add_search_filters(memory_search)
    _add_json_flag(memory_search)

    memory_import = memory_commands.add_parser("import", help="Markdown / JSON / JSONL / 既存Storeを取り込み")
    _add_project_ref(memory_import)
    memory_import.add_argument("--input", required=True)
    memory_import.add_argument("--format", choices=("auto", "markdown", "json", "jsonl", "routecraft"), default="auto")
    _add_json_flag(memory_import)

    memory_export = memory_commands.add_parser("export", help="メモリを書き出し")
    memory_export.add_argument("--project")
    memory_export.add_argument("--format", choices=("json", "jsonl", "markdown"), default="jsonl")
    memory_export.add_argument("--output", required=True)
    memory_export.add_argument("--safe", action="store_true", help="秘密情報と端末固有pathを除外")
    _add_json_flag(memory_export)

    context = commands.add_parser("context", help="Context Pack")
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_build = context_commands.add_parser("build", help="Context Packを生成")
    _add_project_ref(context_build)
    context_build.add_argument("--format", choices=("markdown", "text", "json"), default="markdown")
    context_build.add_argument("--profile", choices=("compact", "standard", "full"), default="standard")
    context_build.add_argument("--max-chars", type=int)
    context_build.add_argument("--max-tokens", type=int)
    context_build.add_argument("--output")
    _add_json_flag(context_build)

    handoff = commands.add_parser("handoff", help="AI間Handoff Pack")
    handoff_commands = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_build = handoff_commands.add_parser("build", help="Handoff Packを生成")
    _add_project_ref(handoff_build)
    handoff_build.add_argument("--output", required=True)
    handoff_build.add_argument("--zip", action="store_true")
    _add_json_flag(handoff_build)

    git_command = commands.add_parser("git", help="Git情報（読み取り専用）")
    git_commands = git_command.add_subparsers(dest="git_command", required=True)
    git_status = git_commands.add_parser("status", help="Git状態を取得")
    _add_project_ref(git_status)
    git_status.add_argument("--recent", type=int, default=10)
    _add_json_flag(git_status)

    session = commands.add_parser("session", help="セッション要約")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_summary = session_commands.add_parser("summarize", help="Git差分からルールベース要約")
    _add_project_ref(session_summary)
    session_summary.add_argument("--save", action="store_true")
    session_summary.add_argument("--importance", choices=IMPORTANCE_LEVELS, default="medium")
    _add_json_flag(session_summary)

    loop = commands.add_parser("loop", help="Codex RouteCraft Loopとのローカル連携")
    loop_commands = loop.add_subparsers(dest="loop_command", required=True)
    loop_status_command = loop_commands.add_parser("status", help="Loop連携設定を表示")
    _add_json_flag(loop_status_command)
    loop_configure = loop_commands.add_parser("configure", help="Loop連携を有効化または無効化")
    loop_mode = loop_configure.add_mutually_exclusive_group(required=True)
    loop_mode.add_argument("--enable", dest="loop_enabled", action="store_true")
    loop_mode.add_argument("--disable", dest="loop_enabled", action="store_false")
    loop_configure.add_argument("--auto-context", action=argparse.BooleanOptionalAction, default=None)
    loop_configure.add_argument("--auto-session-summary", action=argparse.BooleanOptionalAction, default=None)
    loop_configure.add_argument("--context-profile", choices=("compact", "standard", "full"))
    loop_configure.add_argument("--max-context-chars", type=int)
    _add_json_flag(loop_configure)

    status = commands.add_parser("status", help="ローカル状態を表示")
    _add_json_flag(status)
    doctor = commands.add_parser("doctor", help="システム診断")
    _add_json_flag(doctor)

    backup = commands.add_parser("backup", help="DBバックアップを作成")
    backup.add_argument("--output")
    _add_json_flag(backup)
    restore = commands.add_parser("restore", help="DBバックアップから安全に復元")
    restore.add_argument("--input", required=True)
    restore.add_argument("--confirm", required=True, help="RESTORE と完全入力")
    _add_json_flag(restore)

    export = commands.add_parser("export", help="全体またはproject単位を書き出し")
    export.add_argument("--project")
    export.add_argument("--format", choices=("json", "jsonl", "markdown"), default="jsonl")
    export.add_argument("--output", required=True)
    export.add_argument("--safe", action="store_true")
    _add_json_flag(export)

    import_command = commands.add_parser("import", help="project packageを取り込み")
    import_command.add_argument("--input", required=True)
    import_command.add_argument("--conflict", choices=("detect", "skip"), default="detect")
    _add_json_flag(import_command)

    ui = commands.add_parser("ui", help="日本語ローカルWeb UIを起動")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    _add_json_flag(ui)

    return parser


def _bool_filter(value: str) -> bool | None:
    return None if value == "any" else value == "yes"


def _handle(args: argparse.Namespace, service: RouteCraftService, json_mode: bool) -> int:
    command = args.command
    if command == "init":
        result = service.initialize()
        _emit(result, json_mode=json_mode, human=f"初期化しました: {result.get('database', result.get('db_path', ''))}")
        return 0

    if command == "loop":
        if args.loop_command == "status":
            result = loop_status()
            _emit(result, json_mode=json_mode)
            return 0
        result = configure_loop(
            enabled=args.loop_enabled,
            data_dir=args.data_dir,
            auto_context=args.auto_context,
            auto_session_summary=args.auto_session_summary,
            context_profile=args.context_profile,
            max_context_chars=args.max_context_chars,
        )
        if result["enabled"]:
            RouteCraftService(result["data_dir"]).initialize()
        state = "enabled" if result["enabled"] else "disabled"
        _emit(result, json_mode=json_mode, human=f"RouteCraft Memory Local Loop integration: {state}")
        return 0

    service.initialize()

    if command == "project":
        sub = args.project_command
        if sub == "add":
            repo = str(Path(args.repo_path).expanduser().resolve()) if args.repo_path else ""
            git = inspect_git(repo) if repo else {}
            name = args.name or (Path(repo).name if repo else "")
            if not name:
                raise RouteCraftLocalError("--name または --repo が必要です。")
            result = service.add_project(
                name=name,
                repo_path=repo,
                git_remote_url=args.remote_url or str(git.get("remote_url", "")),
                ai_agents=_csv(args.agent),
                languages=_csv(args.language),
                tags=_csv(args.tag),
                description=args.description,
                current_objective=args.objective,
            )
            _emit(result, json_mode=json_mode, human=f"プロジェクトを登録しました: {result['name']} ({result['id']})")
        elif sub == "list":
            result = service.list_projects(include_archived=args.include_archived)
            lines = [f"{item['id']}\t{item['name']}\t{'archived' if item.get('archived') else 'active'}" for item in result]
            _emit(result, json_mode=json_mode, human="\n".join(lines) or "プロジェクトはありません。")
        elif sub == "show":
            result = service.get_project(args.project)
            _emit(result, json_mode=json_mode)
        elif sub == "rename":
            result = service.update_project(args.project, name=args.name)
            _emit(result, json_mode=json_mode, human=f"名前を変更しました: {result['name']}")
        elif sub == "edit":
            changes: dict[str, Any] = {}
            mapping = {
                "repo_path": args.repo,
                "git_remote_url": args.remote_url,
                "description": args.description,
                "current_objective": args.objective,
            }
            changes.update({key: value for key, value in mapping.items() if value is not None})
            if args.agent is not None:
                changes["ai_agents"] = _csv(args.agent)
            if args.language is not None:
                changes["languages"] = _csv(args.language)
            if args.tag is not None:
                changes["tags"] = _csv(args.tag)
            result = service.update_project(args.project, **changes)
            _emit(result, json_mode=json_mode, human=f"プロジェクトを更新しました: {result['id']}")
        elif sub == "archive":
            result = service.archive_project(args.project, archived=not args.undo)
            _emit(result, json_mode=json_mode, human=f"アーカイブ状態: {bool(result.get('archived'))}")
        elif sub == "delete":
            result = service.delete_project(args.project, args.confirm)
            _emit(result, json_mode=json_mode, human=f"削除しました。安全コピー: {result.get('safety_copy', '')}")
        elif sub == "backup":
            result = service.export_project_package(args.project, args.output, as_zip=not args.folder)
            _emit(result, json_mode=json_mode, human=f"作成しました: {result.get('output', args.output)}")
        elif sub == "restore":
            result = service.import_project_package(args.input, conflict=args.conflict)
            _emit(result, json_mode=json_mode)
        return 0

    if command == "memory":
        sub = args.memory_command
        if sub == "add":
            body = _read_body(args.body, args.input_file)
            if not body.strip():
                raise RouteCraftLocalError("本文が空です。--body、--input-file、またはstdinを指定してください。")
            result = service.add_memory(
                args.project,
                args.memory_type,
                args.title,
                body,
                importance=args.importance,
                tags=_csv(args.tag),
                source=args.source,
                related_files=_csv(args.related_files),
                related_commits=_csv(args.related_commits),
                verified=args.verified,
            )
            warning = f" / masking: {', '.join(result.get('warnings', []))}" if result.get("warnings") else ""
            _emit(result, json_mode=json_mode, human=f"メモリを登録しました: {result['id']}{warning}")
        elif sub == "list":
            result = service.list_memories(
                args.project,
                limit=args.limit,
                offset=args.offset,
                include_inactive=args.include_inactive,
                types=_csv(args.types),
                importance=_csv(args.importance),
            )
            lines = [
                f"{item['id']}\t{item.get('memory_type', item.get('type', 'note'))}\t{item['importance']}\t{item['title']}"
                for item in result
            ]
            _emit(result, json_mode=json_mode, human="\n".join(lines) or "メモリはありません。")
        elif sub == "show":
            _emit(service.get_memory(args.id), json_mode=json_mode)
        elif sub == "edit":
            changes: dict[str, Any] = {}
            if args.memory_type is not None:
                changes["memory_type"] = args.memory_type
            if args.title is not None:
                changes["title"] = args.title
            if args.body is not None or args.input_file is not None:
                changes["body"] = _read_body(args.body, args.input_file)
            if args.importance is not None:
                changes["importance"] = args.importance
            if args.tag is not None:
                changes["tags"] = _csv(args.tag)
            if args.related_files is not None:
                changes["related_files"] = _csv(args.related_files)
            if args.related_commits is not None:
                changes["related_commits"] = _csv(args.related_commits)
            if args.active is not None:
                changes["active"] = args.active == "yes"
            if args.verified is not None:
                changes["verified"] = args.verified == "yes"
            result = service.update_memory(args.id, **changes)
            _emit(result, json_mode=json_mode, human=f"更新しました: {result['id']}")
        elif sub == "delete":
            result = service.delete_memory(args.id, args.confirm)
            _emit(result, json_mode=json_mode, human=f"削除しました: {args.id}")
        elif sub == "search":
            result = service.search_memories(
                args.project,
                args.query,
                types=_csv(args.types),
                tags=_csv(args.tags),
                importance=_csv(args.importance),
                created_from=args.created_from,
                created_to=args.created_to,
                filename=args.filename,
                commit=args.commit,
                active=_bool_filter(args.active),
                verified=_bool_filter(args.verified),
                limit=args.limit,
            )
            lines = [
                f"{item.get('relevance', 0):.2f}\t{item.get('memory_type', item.get('type', 'note'))}\t{item['importance']}\t{item['title']}"
                for item in result
            ]
            _emit(result, json_mode=json_mode, human="\n".join(lines) or "該当するメモリはありません。")
        elif sub == "import":
            if args.format == "routecraft":
                result = service.import_routecraft_store(args.project, args.input)
            else:
                result = service.import_file(args.project, args.input, format=args.format)
            _emit(result, json_mode=json_mode)
        elif sub == "export":
            result = service.export_memories(args.project, fmt=args.format, output=args.output, safe=args.safe)
            _emit(result, json_mode=json_mode, human=f"書き出しました: {result.get('output', args.output)}")
        return 0

    if command == "context":
        result = build_context_pack(
            service,
            args.project,
            format=args.format,
            profile=args.profile,
            max_chars=args.max_chars,
            max_tokens=args.max_tokens,
        )
        if args.output:
            target = Path(args.output).expanduser()
            if target.exists():
                raise RouteCraftLocalError("Context Pack の出力先は既に存在します。別のパスを指定してください。")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(result["content"]), encoding="utf-8", newline="\n")
            result["path"] = str(target.resolve())
            _emit(result, json_mode=json_mode, human=f"Context Pack: {result['path']} ({result['char_count']} chars / 約{result['estimated_tokens']} tokens)")
        else:
            _emit(result, json_mode=json_mode, human=str(result["content"]))
        return 0

    if command == "handoff":
        result = build_handoff_pack(service, args.project, args.output, as_zip=args.zip)
        destination = result.get("zip") or result.get("folder") or args.output
        _emit(result, json_mode=json_mode, human=f"Handoff Pack: {destination}")
        return 0

    if command == "git":
        project = service.get_project(args.project)
        result = inspect_git(project.get("repo_path", ""), recent_limit=args.recent)
        _emit(result, json_mode=json_mode)
        return 0

    if command == "session":
        project = service.get_project(args.project)
        summary = rule_based_session_summary(project.get("repo_path", ""))
        result: dict[str, Any] = {"summary": summary}
        if args.save:
            result["memory"] = service.add_memory(
                project["id"],
                "session_summary",
                summary["title"],
                summary["body"],
                importance=args.importance,
                tags=("git", "session"),
                source="git-rule-based",
                related_files=summary.get("related_files", ()),
                related_commits=summary.get("related_commits", ()),
            )
        _emit(result, json_mode=json_mode)
        return 0

    if command == "status":
        result = service.doctor()
        _emit(result, json_mode=json_mode)
        return 0
    if command == "doctor":
        result = service.doctor()
        _emit(result, json_mode=json_mode)
        return 0 if result.get("ok", True) else 1
    if command == "backup":
        result = service.backup(args.output)
        _emit(result, json_mode=json_mode, human=f"バックアップ: {result.get('output', '')}")
        return 0
    if command == "restore":
        result = service.restore(args.input, args.confirm)
        human=f"復元しました。事前バックアップ: {result.get('pre_restore_backup', '')}"
        if result.get("warnings"):
            human += f" / 警告: {', '.join(result['warnings'])}"
            if result.get("retained_rollback"): human += f" / 保持されたrollback: {result['retained_rollback']}"
        _emit(result, json_mode=json_mode, human=human)
        return 0
    if command == "export":
        result = service.export_memories(args.project, fmt=args.format, output=args.output, safe=args.safe)
        _emit(result, json_mode=json_mode, human=f"書き出しました: {result.get('output', args.output)}")
        return 0
    if command == "import":
        result = service.import_project_package(args.input, conflict=args.conflict)
        _emit(result, json_mode=json_mode)
        return 0
    if command == "ui":
        from .ui import run_ui

        if json_mode:
            raise RouteCraftLocalError("長時間起動する ui コマンドでは --json を使用できません。")
        return run_ui(service, port=args.port, open_browser=not args.no_browser)
    raise RouteCraftLocalError(f"未対応のコマンドです: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "global_json", False) or getattr(args, "json", False))
    try:
        service = RouteCraftService(args.data_dir)
        return _handle(args, service, json_mode)
    except RouteCraftLocalError as exc:
        payload = {"ok": False, "error": {"code": exc.__class__.__name__, "message": str(exc)}}
        if json_mode:
            print(_json_text(payload))
        else:
            print(f"routecraft: {exc}", file=sys.stderr)
        return int(getattr(exc, "exit_code", 2))
    except KeyboardInterrupt:
        if json_mode:
            print(_json_text({"ok": False, "error": {"code": "Interrupted", "message": "中断されました。"}}))
        else:
            print("routecraft: 中断されました。", file=sys.stderr)
        return 130
    except Exception as exc:  # Defensive CLI boundary; debug mode preserves traceback.
        if os.environ.get("ROUTECRAFT_DEBUG") == "1":
            raise
        message = f"予期しないエラーです。ROUTECRAFT_DEBUG=1 で詳細確認できます: {exc.__class__.__name__}"
        if json_mode:
            print(_json_text({"ok": False, "error": {"code": "InternalError", "message": message}}))
        else:
            print(f"routecraft: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
