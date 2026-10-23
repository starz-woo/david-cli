"""Plain terminal interface. Untrusted text is never rendered as terminal control."""
import sys
import unicodedata
import argparse
from pathlib import Path
from .config import load_settings, ConfigError


def console_main(argv=None):
    """Installed console command: user settings and the invoking directory."""
    return main(argv, installed=True)


def main(argv=None, *, installed=False):
    parser = argparse.ArgumentParser(description='David — internal LLM coding CUI (Python 3.11+)')
    default_settings = Path.home() / '.david' / 'setting.json' if installed else Path(__file__).resolve().parent.parent / 'setting.json'
    parser.add_argument('--settings', type=Path, default=default_settings)
    parser.add_argument('--workspace', type=Path, default=Path.cwd() if installed else None,
                        help='Workspace directory (installed david command defaults to current directory)')
    parser.add_argument('--config-path', action='store_true', help='Print selected settings path without reading it or connecting')
    if installed:
        parser.add_argument('--init', action='store_true', help='Create user setting.json if absent; never overwrite')
    parser.add_argument('--check', action='store_true', help='Validate JSON settings offline; no server request')
    parser.add_argument('--prompt', help='Run one request; no automatic approval')
    parser.add_argument('--skill', action='append', dest='skill_names', metavar='NAME',
                        help='Select a registered skill (repeatable; overrides enabled skills for this session)')
    parser.add_argument('--read-only', action='store_true', help='Disable writes, commands, and MCP for this session')
    interface = parser.add_mutually_exclusive_group()
    interface.add_argument('--plain', action='store_true', help='Use legacy line console (no TUI/session UI)')
    interface.add_argument('--tui', action='store_true', help='Use full-screen terminal UI (default on interactive terminals)')
    args = parser.parse_args(argv)
    try:
        args.settings = args.settings.expanduser().absolute()
        if args.config_path:
            print(safe_text(args.settings))
            return 0
        if installed and args.init:
            import json
            import os
            from .config import DEFAULTS
            args.settings.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(args.settings, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                print('Existing settings kept: ' + safe_text(args.settings))
                return 0
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + '\n')
            print('Created: ' + safe_text(args.settings))
            print('Edit model.base_url and model.name before starting david.')
            return 0
        if installed and not args.settings.exists():
            raise ConfigError('Settings not found. Run david --init, then edit the file shown by david --config-path.')
        settings = load_settings(args.settings)
        if args.skill_names is not None:
            from .extensions import validate_skill_selection
            validate_skill_selection(args.skill_names, settings['skills']['registered'])
        if args.check:
            print('OFFLINE: JSON settings valid. Server/model/tool compatibility NOT tested.')
            return 0
        if args.workspace is None:
            raise ConfigError('--workspace is required; select the project explicitly.')
        if args.prompt is None and not args.plain and (args.tui or (sys.stdin.isatty() and sys.stdout.isatty())):
            try:
                from .tui import run_tui
            except ImportError as exc:
                raise ConfigError('TUI dependencies are missing. Install the bundled wheels, or use --plain.') from exc
            from .controller import Controller
            controller = Controller(args.settings, args.workspace, args.read_only, skill_names=args.skill_names)
            try:
                return run_tui(controller)
            finally:
                controller.close()
        options = {} if args.skill_names is None else {'skill_names': args.skill_names}
        agent = build_session(args.settings, args.workspace, args.read_only, **options)
        return _plain_session(agent, args)
    except Exception as exc:
        print('ERROR: ' + safe_text(exc), file=sys.stderr)
        return 2


def _plain_session(agent, args):
    from . import __version__
    from .extensions import extension_listing, select_skills
    print('David ' + __version__ + ' | ' + safe_text(agent.settings['model']['name']))
    print('Workspace: ' + safe_text(args.workspace.absolute()))
    print('Code/tool output will be sent to the configured server. Commands and MCP are NOT OS-sandboxed.')
    print('/help 명령 목록 · /reload 설정 재적용 및 대화 초기화 · /quit 종료')
    try:
        if args.prompt is not None:
            return run_turn(agent, args.prompt)
        while True:
            try:
                prompt = input('\nyou> ').strip()
            except (EOFError, KeyboardInterrupt):
                print('\nBye.')
                return 0
            if not prompt:
                continue
            if prompt in ('/quit', '/exit'):
                return 0
            if prompt == '/reload':
                try:
                    replacement = build_session(args.settings, args.workspace, args.read_only,
                                                skill_names=args.skill_names)
                    previous, agent = agent, replacement
                    previous.tools.close()
                    print('Reloaded. Conversation cleared; earlier disk changes remain.')
                except Exception as exc:
                    print('Reload failed; previous settings kept: ' + safe_text(exc))
                continue
            if prompt == '/clear':
                agent.history.clear()
                print('Conversation cleared. Disk changes remain.')
                continue
            if prompt == '/help':
                print('/help /settings /tools /mcp /skills /skill NAME|off /clear /reload /quit\n'
                      'Use --skill NAME (repeatable) for one-shot selection. Skills are text, not scripts.\n'
                      'MCP launch/calls require approval and are NOT sandboxed. Approvals require exactly yes.')
                continue
            if prompt == '/settings':
                show_settings(agent.settings)
                continue
            if prompt == '/tools':
                print('\n'.join(s['function']['name'] for s in agent.tools.schemas()))
                continue
            if prompt in ('/mcp', '/skills'):
                print(safe_text(extension_listing(agent, prompt)))
                continue
            if prompt.split(None, 1)[0] == '/skill':
                try:
                    parts = prompt.split(None, 1)
                    if len(parts) != 2:
                        raise ValueError('Use /skill NAME or /skill off. List names with /skills.')
                    names = [] if parts[1] == 'off' else [parts[1]]
                    select_skills(agent, names)
                    args.skill_names = names
                    print('Skills changed. Conversation cleared; managed workspace undo and disk changes remain.')
                except Exception as exc:
                    print('Skill selection failed; prior selection kept: ' + safe_text(exc))
                continue
            if prompt.startswith('/'):
                print('Unknown command. Use /help.')
                continue
            run_turn(agent, prompt)
    finally:
        agent.tools.close()


def show_settings(settings):
    from urllib.parse import urlsplit
    model = settings['model']
    host = urlsplit(model['base_url']).hostname
    print('Model: ' + safe_text(model['name']) + ' | Host: ' + safe_text(host))
    print('Tool mode: ' + settings['agent']['tool_mode'])
    print('Read-only: ' + str(settings['security']['read_only']) + ' | Commands: ' + str(settings['security']['enable_commands']))


def run_turn(agent, prompt):
    print('\ndavid> ', end='', flush=True)
    try:
        result = agent.run(prompt)
        print(f'\n[model calls={result.model_calls}, elapsed={result.elapsed_seconds:.2f}s]')
        return 0
    except KeyboardInterrupt:
        agent.history.clear()
        print('\nInterrupted. Conversation cleared; completed changes remain on disk.')
        return 130
    except Exception as exc:
        agent.history.clear()
        print('\nERROR: ' + safe_text(exc), file=sys.stderr)
        print('Conversation cleared; completed changes remain on disk.', file=sys.stderr)
        return 1


def build_session(settings_path, workspace, read_only=False, *, skill_names=None,
                  manager_factory=None, registry_factory=None):
    from .agent import Agent
    from .transport import OpenAIClient
    from .workspace import WorkspaceTools
    import json
    settings_path = Path(settings_path).expanduser().resolve()
    settings = load_settings(settings_path)
    if settings['model']['name'] == 'CHANGE_ME':
        raise ConfigError('Set model.name and model.base_url in setting.json first.')
    if read_only:
        settings['security']['read_only'] = True
        settings['security']['enable_commands'] = False
    if not Path(workspace).expanduser().is_dir():
        raise ConfigError('Workspace must be an existing directory.')
    package = Path(__file__).resolve().parent
    protected = [settings_path, package, package.parent / 'david.py']
    from .config import TOKENIZER_PATHS
    for model_config in (settings['model'], *settings.get('model_profiles', {}).values()):
        protected.extend(Path(model_config[k]).resolve() for k in TOKENIZER_PATHS if model_config.get(k))
    if sys.argv and Path(sys.argv[0]).is_file():
        protected.append(Path(sys.argv[0]).resolve())
    extra_prompt = ''
    prompt_file = settings['agent']['system_prompt_file']
    if prompt_file:
        prompt_path = (settings_path.parent / prompt_file).resolve()
        if prompt_path.stat().st_size > 16384:
            raise ConfigError('Custom system prompt exceeds 16 KiB.')
        extra_prompt = prompt_path.read_text(encoding='utf-8-sig')
        protected.append(prompt_path)
    from .extensions import ExtensionTools
    if registry_factory is None:
        from .skills import SkillRegistry
        registry_factory = SkillRegistry
    registry = registry_factory(settings['skills']['registered'], base_dir=settings_path.parent)
    selected = list(settings['skills']['enabled'] if skill_names is None else skill_names)
    from .extensions import validate_skill_selection
    validate_skill_selection(selected, settings['skills']['registered'])
    skill_text = registry.load(selected)
    protected.extend(registry.protected_paths())
    base_tools = WorkspaceTools(Path(workspace).expanduser(), settings['security'], settings['commands'], approve,
                                protected_paths=protected)
    if manager_factory is None and settings['mcp']['servers']:
        from .mcp import MCPManager
        manager_factory = MCPManager
    manager = (manager_factory(servers=settings['mcp']['servers'], workspace=base_tools.root,
                               approve=approve, cancel_check=lambda: None,
                               read_only=settings['security']['read_only'])
               if manager_factory is not None else None)
    tools = ExtensionTools(base_tools, manager, settings['mcp']['servers'])
    def show_tool(name, result):
        preview = json.dumps(result, ensure_ascii=False)
        print('\n[tool ' + safe_text(name) + '] ' + safe_text(preview[:1200]) + (' ...' if len(preview) > 1200 else ''))
    try:
        agent = Agent(OpenAIClient(settings['model']), tools, settings,
                      extra_prompt=extra_prompt + ('\n' + skill_text if skill_text else ''),
                      on_text=lambda text: print(safe_text(text), end='', flush=True), on_tool=show_tool)
    except BaseException:
        tools.close()
        raise
    agent.skill_registry = registry
    agent.skill_names = selected
    agent.base_extra_prompt = extra_prompt
    agent.settings['skills']['enabled'] = list(selected)
    return agent


def safe_text(value):
    return ''.join(c if c in '\n\t' or unicodedata.category(c) not in ('Cc', 'Cf', 'Cs')
                   else f'\\u{ord(c):04x}' for c in str(value))


def approve(action, detail):
    print('\n--- APPROVAL: ' + safe_text(action) + ' ---')
    print(safe_text(detail))
    if not sys.stdin.isatty():
        print('DENIED: approvals require an interactive terminal.')
        return False
    try:
        return input('허용하려면 yes 입력 (그 외 거부): ').strip() == 'yes'
    except EOFError:
        print('\nDENIED')
        return False
