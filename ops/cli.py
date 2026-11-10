"""Lazy CLI integration, safe JSON output and audit recording."""
import json
import os
import traceback


COMMANDS = ('plan', 'draft', 'compliance', 'schedule', 'publish-guide', 'measure', 'package')


def add_parser(subparsers, handler):
    parser = subparsers.add_parser('ops', help='DeepSeek operations; local queue and human publish guides only')
    commands = parser.add_subparsers(dest='ops_command', required=True)
    for name in COMMANDS:
        command = commands.add_parser(name)
        command.add_argument('--project', default='hanset')
        modes = command.add_mutually_exclusive_group()
        modes.add_argument('--dry-run', action='store_true')
        modes.add_argument('--mock-file')
        command.add_argument('--draft-id', type=int)
        command.add_argument('--limit', type=int)
        command.add_argument('--ops-draft-id', dest='ops_draft_ids', action='append', type=int)
        command.add_argument('--ops-schedule-id', dest='ops_schedule_ids', action='append', type=int)
        command.add_argument('--post-id')
        if name == 'package':
            command.add_argument('--packet', required=True)
            command.add_argument('--date', required=True)
            command.add_argument('--out-dir')
        if name == 'plan':
            command.add_argument('--mode', choices=('daily', 'next-week'), default='daily')
        if name == 'measure':
            command.add_argument('--check-kind', choices=('48h', '7d'))
        command.set_defaults(func=handler)


def run(args):
    import logging
    import sys
    import autobot as ab
    from . import guards, pipeline
    from .config import resolve_path
    from .storage import ensure_ops_schema
    conn = None
    run_id = None
    cfg = None
    now = ab.now_kst()
    mode = 'dry-run' if args.dry_run else 'mock' if args.mock_file else 'live'
    try:
        cfg = ab.load_config(args.config)
        guards.register_secret(os.environ.get(cfg['ops']['measure']['x_bearer_env'], ''))
        ab.setup_logging(cfg, getattr(args, 'verbose', False))
        guards.install_redaction(ab.log, [os.environ.get('DEEPSEEK_API_KEY', ''), os.environ.get(cfg['ops']['measure']['x_bearer_env'], '')])
        # Existing metrics helpers log progress; stdout is reserved for the JSON result.
        for handler in ab.log.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setStream(sys.stderr)
        conn = ab.db_connect(cfg)
        ensure_ops_schema(conn)
        prompt_name = 'plan_next' if getattr(args, 'mode', None) == 'next-week' else args.ops_command
        template = resolve_path(os.path.join(cfg['ops']['paths']['prompts'], prompt_name + '.md')) if args.ops_command in ('plan', 'draft', 'compliance', 'measure', 'package') else None
        run_id = pipeline.begin_run(conn, args.ops_command, args.project, cfg, mode=mode, prompt_path=template, now=now)
        proj = next((p for p in cfg['_projects'] if p.name == args.project), None)
        if not proj:
            raise guards.OpsBlocked('brand_not_supported')
        mock = None
        if args.mock_file:
            try:
                with open(resolve_path(args.mock_file), encoding='utf-8') as stream:
                    mock = json.load(stream)
                if not isinstance(mock, dict):
                    raise ValueError('object required')
            except (ValueError, OSError):
                raise guards.OpsLLMError('llm_output_invalid') from None
        function = getattr(pipeline, 'cmd_' + args.ops_command.replace('-', '_'))
        result = function(conn, cfg, proj, now, dry_run=args.dry_run, mock=mock, draft_id=args.draft_id,
                          limit=args.limit, ops_draft_ids=args.ops_draft_ids, ops_schedule_ids=args.ops_schedule_ids,
                          post_id=args.post_id, run_id=run_id, packet=getattr(args, 'packet', None),
                          date=getattr(args, 'date', None), out_dir=getattr(args, 'out_dir', None),
                          mode=getattr(args, 'mode', 'daily'), check_kind=getattr(args, 'check_kind', None))
        rc = 2 if result['status'] == 'blocked' else 0
    except guards.OpsError as exc:
        if conn:
            conn.rollback()
        result = {'status': 'blocked', 'reason': guards.redact(str(exc))}
        rc = 2
    except Exception:
        if conn:
            conn.rollback()
        # Redact the full traceback before sending it to any sink.
        print(guards.redact(traceback.format_exc()), file=sys.stderr)
        result = {'status': 'error', 'reason': 'internal_error'}
        rc = 1
    try:
        result = guards.redact_obj(result)
        if conn is not None and run_id is not None:
            pipeline.finish_run(conn, run_id, result['status'], output=result, error=result.get('reason'), now=now)
        if cfg is not None and mode in ('dry-run', 'mock'):
            pipeline.write_dryrun_log(cfg, {'command': args.ops_command, 'mode': mode, 'output': result})
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        if conn is not None:
            conn.close()
    return rc
