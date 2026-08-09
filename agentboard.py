#!/usr/bin/env python3
"""Agent Board — пространственная доска агентов Claude Code поверх tmux.

Карточка = один разговор (агент). Всё, что на доске, автоматически
сохраняется в board.json и переживает перезагрузку: живая карточка после
ребута становится «на паузе», кнопка «продолжить» возобновляет разговор
(claude --resume). «Убрать с доски» снимает карточку, не трогая историю
Claude Code — вернуть можно через «+» из истории.

Запуск: python3 agentboard.py → http://localhost:8787
"""
import base64
import fcntl
import glob
import hashlib
import json
import os
import pty
import re
import struct
import termios
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

__version__ = "0.2.2"

PORT = int(os.environ.get("AGENTBOARD_PORT", "8787"))
# кем доска соглашается себя считать: всё прочее в Host/Origin — чужой сайт
LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))
# в чём открывать сессию по клику. Warp идёт своим путём (см. _open_in_warp),
# Terminal и iTerm2 берут .command-файл
TERMINAL_APP = os.environ.get("AGENTBOARD_TERMINAL", "Terminal")
# бандл-версия (.app) приносит свой tmux и живёт на своём сокете,
# чтобы не пересекаться с юзерским tmux-сервером (protocol version mismatch)
TMUX = os.environ.get("AGENTBOARD_TMUX") or shutil.which("tmux") or "/opt/homebrew/bin/tmux"
TMUX_SOCKET = os.environ.get("AGENTBOARD_TMUX_SOCKET", "")
TMUX_CMD = [TMUX] + (["-L", TMUX_SOCKET] if TMUX_SOCKET else [])
# панели tmux наследуют env сервера, а не шелла — задаём PATH явно,
# иначе внутри агентов не находится `claude` и падает его автообновление
AGENT_PATH = ":".join([
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.opencode/bin"),
    "/opt/homebrew/bin", "/usr/local/bin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
])
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
CODEX = shutil.which("codex") or "/opt/homebrew/bin/codex"
CURSOR = shutil.which("cursor-agent") or os.path.expanduser("~/.local/bin/cursor-agent")
OPENCODE = shutil.which("opencode") or os.path.expanduser("~/.opencode/bin/opencode")
STATUS_DIR = os.path.expanduser("~/.claude/agent-status")
NAMES_DIR = f"/tmp/agentboard-{os.getuid()}-names"  # сюда агент первой командой пишет имя карточки
# Warp открывает вкладку без команды — команду ей передаём через этот файл (см. WARP_ZSHRC)
WARP_ATTACH_FILE = f"/tmp/agentboard-{os.getuid()}-warp-attach"
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
# в бандле HERE — read-only внутри .app; данные живут отдельно (AGENTBOARD_DATA)
DATA_DIR = os.environ.get("AGENTBOARD_DATA") or HERE
os.makedirs(DATA_DIR, exist_ok=True)
BOARD_FILE = os.path.join(DATA_DIR, "board.json")

# служебный хром TUI Claude Code — не показываем в превью карточки
CHROME_SNIPPETS = ("⏵⏵", "shift+tab", "esc to interrupt", "/rc active",
                   "← for agents", "auto mode", "plan mode", "bypass permissions",
                   "tokens left", "Context left", "Jump to bottom",
                   "ctrl+o for transcript", "Auto-update failed")
FRAME_CHARS = set("─│╭╮╰╯┌┐└┘━┃╍═║ ")


def is_chrome(line):
    s = line.replace(" ", " ").strip()  # TUI сыплет неразрывными пробелами
    if not s:
        return True
    if set(s) <= FRAME_CHARS:
        return True  # рамки и разделители
    if len(s) >= 4 and sum(c in FRAME_CHARS for c in s) / len(s) > 0.6:
        return True  # строка в основном из линий (обрывки рамок)
    if s.startswith(("❯", "⏸", "▐", "▝", "▘", "⧉", "›")):
        return True  # строка ввода (claude/codex), рекап, баннер, бейджи
    # футер и баннер codex: "gpt-5.6 high fast · ~/path", "model:", ">_ OpenAI Codex"
    if " · ~/" in s or s.startswith(("model:", "directory:", ">_")):
        return True
    if s.startswith("──") or s.endswith("──"):
        return True  # разделитель с именем сессии
    if s.startswith("⏵") and "⏵⏵" in s:
        return True  # футер-подсказка; одиночный ⏵ (шаг работы) оставляем
    if "Claude Code v" in s or "Claude Max" in s:
        return True
    if any(p in s for p in CHROME_SNIPPETS):
        return True
    if s.lstrip("⎿ ").startswith(("Tip:", "※ Tip")):
        return True  # советы TUI
    if s.startswith("Fable ") or s in ("Fable 5", "Opus", "Sonnet"):
        return True  # статус-строка с именем модели
    return False

last_seen = {}   # tmux-сессия -> {"hash", "changed"} для эвристики "работает"

# пока хоть один агент работает, держим caffeinate -i: мак не уснёт от простоя
# (экрану гаснуть можно). Умер сервер — caffeinate выйдет сам благодаря -w.
caffeinate = None


def update_caffeinate(active):
    global caffeinate
    if active and caffeinate is None:
        try:
            caffeinate = subprocess.Popen(
                ["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            pass
    elif not active and caffeinate is not None:
        caffeinate.terminate()
        caffeinate = None
meta_cache = {}  # путь jsonl -> (mtime, cwd, title)
activity_cache = {}  # (путь, типы записей) -> (mtime, timestamp последнего события)
model_cache = {}     # (путь, агент) -> (mtime, модель)

MODEL_LABELS = {
    "fable": "Fable 5",
    "opus": "Opus 4.8",
    "sonnet": "Sonnet 5",
    "haiku": "Haiku 4.5",
    "claude-fable-5": "Fable 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-sonnet-5": "Sonnet 5",
    "claude-haiku-4-5": "Haiku 4.5",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-codex": "GPT-5.6 Codex",
    "composer-2.5": "Composer 2.5",
    "composer-2.5-fast": "Composer 2.5 Fast",
    "openrouter/moonshotai/kimi-k3": "Kimi K3",
    "moonshotai/kimi-k3": "Kimi K3",
}


# Из Finder приложение стартует с обрезанным окружением: ни LANG, ни LC_*.
# Без них tmux считает терминал 8-битным и разбирает UTF-8 побайтно — кириллица
# в send-keys превращается в «ÑÐºÐ°Ð¶Ð¸». Правим только LC_CTYPE: он отвечает
# за кодировку и не трогает язык сообщений самих агентов.
TMUX_ENV = dict(os.environ)
if not any(TMUX_ENV.get(k) for k in ("LC_ALL", "LC_CTYPE", "LANG")):
    TMUX_ENV["LC_CTYPE"] = "en_US.UTF-8"
# та же локаль нужна и внутри сессии — иначе её унаследует сам агент
LOCALE_EXPORT = "export LC_CTYPE=" + shlex.quote(
    TMUX_ENV.get("LC_ALL") or TMUX_ENV.get("LC_CTYPE") or TMUX_ENV.get("LANG"))


def tmux(*args):
    try:
        r = subprocess.run([*TMUX_CMD, *args], capture_output=True, text=True,
                           timeout=5, env=TMUX_ENV)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def tmux_ok(*args):
    try:
        return subprocess.run([*TMUX_CMD, *args], capture_output=True,
                              timeout=5, env=TMUX_ENV).returncode == 0
    except Exception:
        return False


def hook_status(name):
    """Статус из файла хука + его возраст (когда хук последний раз писал)."""
    try:
        p = os.path.join(STATUS_DIR, name)
        with open(p) as f:
            return f.read().strip(), os.path.getmtime(p)
    except OSError:
        return "", 0


started_cache = {}  # путь jsonl -> когда разговор начался


def log_started(path):
    """Timestamp первой записи разговора. Первая строка файла уже не изменится,
    так что кешируем навсегда. Компакшн форкает разговор в новый файл — там
    отсчёт начнётся заново, это и есть новый разговор."""
    if not path:
        return 0
    if path in started_cache:
        return started_cache[path]
    stamp = 0
    try:
        with open(path, errors="ignore") as f:
            for _ in range(50):  # в голове файла попадаются служебные записи
                line = f.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                raw = row.get("timestamp")
                if raw:
                    stamp = int(datetime.fromisoformat(
                        raw.replace("Z", "+00:00")).timestamp())
                    break
    except OSError:
        return 0
    started_cache[path] = stamp
    return stamp


def log_activity(path, record_types=()):
    """Timestamp последнего настоящего события внутри JSONL, а не mtime файла."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 0
    key = (path, record_types)
    hit = activity_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    latest = 0
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if record_types and row.get("type") not in record_types:
                        continue
                    stamp = row.get("timestamp")
                    if stamp:
                        latest = max(latest, int(datetime.fromisoformat(
                            stamp.replace("Z", "+00:00")).timestamp()))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return 0
    activity_cache[key] = (mtime, latest)
    return latest


def log_model(path, agent):
    """Модель из самого лога сессии; работает и для старых карточек."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (path, agent)
    hit = model_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    model = ""
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if agent == "claude" and row.get("type") == "assistant":
                    found = row.get("message", {}).get("model", "")
                    if found and found != "<synthetic>":
                        model = found
                elif agent == "codex" and row.get("type") == "turn_context":
                    found = row.get("payload", {}).get("model", "")
                    if found and found != "codex-auto-review":
                        model = found
    except OSError:
        return ""
    model_cache[key] = (mtime, model)
    return model


def model_label(model):
    return MODEL_LABELS.get(model, model)


# ---------- статус-хуки: установка в конфиги Claude Code и Codex ----------
#
# Оба CLI умеют lifecycle-хуки одинакового вида. Хук пишет статус в
# ~/.claude/agent-status/<tmux-сессия>, доска его читает. PermissionRequest —
# штатное событие «появился диалог разрешения» (Claude Code 2.x, Codex 0.122+).

CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX_HOOKS_FILE = os.path.expanduser("~/.codex/hooks.json")
HOOK_MARK = "agent-status"  # по этой подстроке узнаём свои хуки в чужом конфиге

HOOK_EVENTS = {  # событие CLI -> статус на доске
    "PermissionRequest": "waiting",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",   # разрешение получено, тул пошёл — снимаем «жду»
    "PostToolUse": "working",
    "Stop": "idle",
}


def hook_cmd(status):
    """Команда хука: пишет статус и для tmux-агента, и для сессии вне его.

    Внутри tmux ключ — имя сессии (его знает и tmux, и доска). Снаружи tmux
    ничего общего нет, кроме sessionId: он приходит хуку в JSON на stdin,
    вытаскиваем его sed'ом (jq есть не у всех, python3 на каждый PreToolUse
    дороговат). Ключ такой записи — ext-<sessionId>, как в external_agents."""
    return ('d=~/.claude/agent-status; mkdir -p "$d"; '
            's=$(tr -d "\\n" | sed -n \'s/.*"session_id"[^"]*"\\([^"]*\\)".*/\\1/p\'); '
            f'[ -n "$TMUX" ] && echo {status} > "$d/$(tmux display -p \'#S\')"; '
            f'[ -n "$s" ] && echo {status} > "$d/ext-$s"; true')


def _hooks_missing(cfg):
    """Каких событий из HOOK_EVENTS нет в блоке hooks конфига."""
    hooks = cfg.get("hooks") or {}
    return [ev for ev in HOOK_EVENTS
            if not any(HOOK_MARK in h.get("command", "")
                       for grp in hooks.get(ev, []) or []
                       for h in grp.get("hooks", []) or [])]


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f), True
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError):
        return {}, False  # битый конфиг — не трогаем


def hooks_state():
    claude_cfg, claude_ok = _read_json(CLAUDE_SETTINGS)
    codex_cfg, codex_ok = _read_json(CODEX_HOOKS_FILE)
    cursor_cfg, cursor_ok = _read_json(CURSOR_HOOKS_FILE)
    # «установлено» = хуки статусов + секция самоименования в глобальной памяти
    return {
        "claude": claude_ok and not _hooks_missing(claude_cfg) and _md_installed(CLAUDE_MD),
        "codex": codex_ok and not _hooks_missing(codex_cfg) and _md_installed(CODEX_MD),
        "cursor": cursor_ok and not _cursor_missing(cursor_cfg) and _cursor_script_ok(),
        "opencode": _opencode_plugin_ok() and _md_installed(OPENCODE_MD),
    }


# ---- Cursor: свой формат hooks.json + скрипт (нужен разбор stdin) ----
# У Cursor нет события «жду разрешения», поэтому waiting ставим перед каждым
# shell/MCP-вызовом и снимаем после: одобренная команда снимает его сама, а
# неодобренная так и держит карточку жёлтой. Заодно скрипт молча разрешает
# команду самоименования tee — без него первый же шаг агента упирался бы
# в диалог. Скрипт лежит рядом с конфигом, имя содержит HOOK_MARK.

CURSOR_HOOKS_FILE = os.path.expanduser("~/.cursor/hooks.json")
CURSOR_HOOK_SCRIPT = os.path.expanduser("~/.cursor/agent-status.sh")
CURSOR_HOOK_EVENTS = {
    "beforeSubmitPrompt": "working",
    "beforeShellExecution": "shell",
    "afterShellExecution": "working",
    "beforeMCPExecution": "shell",
    "afterMCPExecution": "working",
    "postToolUse": "working",
    "stop": "idle",
}
CURSOR_HOOK_SCRIPT_TEXT = """#!/bin/sh
# Agent Board: статус агента для доски (ставится с доски, см. agentboard.py)
IN=$(cat)
case "$IN" in  # команду самоименования разрешаем всегда, tmux ей не нужен
*"tee /tmp/agentboard-"*) [ "$1" = shell ] && { printf '{"permission":"allow"}'; exit 0; };;
esac
[ -n "$TMUX" ] || exit 0
S=$(tmux display -p '#S' 2>/dev/null)
[ -n "$S" ] || exit 0
D="$HOME/.claude/agent-status"; mkdir -p "$D"
if [ "$1" = shell ]; then
  echo waiting > "$D/$S"
else
  echo "$1" > "$D/$S"
fi
exit 0
"""


def _cursor_missing(cfg):
    hooks = cfg.get("hooks") or {}
    return [ev for ev in CURSOR_HOOK_EVENTS
            if not any(HOOK_MARK in h.get("command", "")
                       for h in hooks.get(ev, []) or [])]


def _cursor_script_ok():
    try:
        with open(CURSOR_HOOK_SCRIPT) as f:
            return f.read() == CURSOR_HOOK_SCRIPT_TEXT
    except OSError:
        return False


CURSOR_CLI_CONFIG = os.path.expanduser("~/.cursor/cli-config.json")


def _install_cursor():
    if not _cursor_script_ok():
        os.makedirs(os.path.dirname(CURSOR_HOOK_SCRIPT), exist_ok=True)
        with open(CURSOR_HOOK_SCRIPT, "w") as f:
            f.write(CURSOR_HOOK_SCRIPT_TEXT)
        os.chmod(CURSOR_HOOK_SCRIPT, 0o755)
    # tee-имя без диалога: hook-ответ beforeShellExecution курсор игнорирует,
    # поэтому штатный allowlist; tee — та же запись файла, что и его edit-тул
    cfg, ok = _read_json(CURSOR_CLI_CONFIG)
    if ok and "Shell(tee)" not in (cfg.get("permissions", {}).get("allow") or []):
        if os.path.exists(CURSOR_CLI_CONFIG) and \
                not os.path.exists(CURSOR_CLI_CONFIG + ".agentboard-bak"):
            shutil.copy2(CURSOR_CLI_CONFIG, CURSOR_CLI_CONFIG + ".agentboard-bak")
        cfg.setdefault("permissions", {}).setdefault("allow", []).append("Shell(tee)")
        tmp = CURSOR_CLI_CONFIG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, CURSOR_CLI_CONFIG)
    cfg, ok = _read_json(CURSOR_HOOKS_FILE)
    if not ok:
        return
    missing = _cursor_missing(cfg)
    if not missing:
        return
    if os.path.exists(CURSOR_HOOKS_FILE) and \
            not os.path.exists(CURSOR_HOOKS_FILE + ".agentboard-bak"):
        shutil.copy2(CURSOR_HOOKS_FILE, CURSOR_HOOKS_FILE + ".agentboard-bak")
    cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    for ev in missing:
        hooks.setdefault(ev, []).append(
            {"command": f"{CURSOR_HOOK_SCRIPT} {CURSOR_HOOK_EVENTS[ev]}"})
    tmp = CURSOR_HOOKS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CURSOR_HOOKS_FILE)


# ---- opencode: плагин со статус-событиями + разрешение на tee в конфиге ----

OPENCODE_PLUGIN = os.path.expanduser("~/.config/opencode/plugins/agent-status.js")
# пишем в тот конфиг, что уже есть у юзера (jsonc с комментариями не парсим — пропустим)
OPENCODE_CONFIG = next(
    (p for p in (os.path.expanduser("~/.config/opencode/opencode.json"),
                 os.path.expanduser("~/.config/opencode/opencode.jsonc"))
     if os.path.exists(p)),
    os.path.expanduser("~/.config/opencode/opencode.json"))
OPENCODE_TEE = "tee /tmp/agentboard-*"
OPENCODE_PLUGIN_TEXT = """// Agent Board: agent-status — пишет статус агента для доски (см. agentboard.py)
import { execSync } from "node:child_process"
import fs from "node:fs"
import os from "node:os"

const MAP = {
  "permission.asked": "waiting",
  "permission.replied": "working",
  "tool.execute.after": "working",
  "message.part.updated": "working",
  "session.idle": "idle",
}

function write(status) {
  try {
    if (!process.env.TMUX) return
    const s = execSync("tmux display -p '#S'", { timeout: 3000 }).toString().trim()
    if (!s) return
    const dir = os.homedir() + "/.claude/agent-status"
    fs.mkdirSync(dir, { recursive: true })
    fs.writeFileSync(dir + "/" + s, status)
  } catch {}
}

export const AgentBoardStatus = async () => ({
  event: async ({ event }) => {
    const status = MAP[event && event.type]
    if (status) write(status)
  },
})
"""


def _opencode_plugin_ok():
    try:
        with open(OPENCODE_PLUGIN) as f:
            return f.read() == OPENCODE_PLUGIN_TEXT
    except OSError:
        return False


def _install_opencode():
    if not _opencode_plugin_ok():
        os.makedirs(os.path.dirname(OPENCODE_PLUGIN), exist_ok=True)
        with open(OPENCODE_PLUGIN, "w") as f:
            f.write(OPENCODE_PLUGIN_TEXT)
    # tee-имя без диалога разрешения (как --settings у claude)
    cfg, ok = _read_json(OPENCODE_CONFIG)
    if not ok:
        return
    perm = cfg.setdefault("permission", {})
    bash = perm.get("bash")
    if bash == "allow" or (isinstance(bash, dict) and OPENCODE_TEE in bash):
        return
    if isinstance(bash, str):  # строка-политика юзера — переносим в шаблоны
        perm["bash"] = {OPENCODE_TEE: "allow", "*": bash}
    elif isinstance(bash, dict):
        bash[OPENCODE_TEE] = "allow"
    else:
        perm["bash"] = {OPENCODE_TEE: "allow"}
    if os.path.exists(OPENCODE_CONFIG) and \
            not os.path.exists(OPENCODE_CONFIG + ".agentboard-bak"):
        shutil.copy2(OPENCODE_CONFIG, OPENCODE_CONFIG + ".agentboard-bak")
    os.makedirs(os.path.dirname(OPENCODE_CONFIG), exist_ok=True)
    tmp = OPENCODE_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, OPENCODE_CONFIG)


def _install_into(path, extra=None):
    """Дописать недостающие хуки в JSON-конфиг. Чужое не трогаем, бэкап один раз."""
    cfg, ok = _read_json(path)
    if not ok:
        return False
    changed = False
    hooks = cfg.setdefault("hooks", {})
    # свои хуки прежних версий убираем целиком: старый Notification с грепом по
    # тексту (он и был поломкой) и всё, что не совпало с текущей командой, —
    # иначе обновление доски не доезжает, _hooks_missing видит метку и молчит
    fresh = {hook_cmd(s) for s in HOOK_EVENTS.values()}
    for ev in list(hooks):
        for grp in list(hooks.get(ev) or []):
            cmds = [h.get("command", "") for h in grp.get("hooks", []) or []]
            if any(HOOK_MARK in c and c not in fresh for c in cmds):
                hooks[ev].remove(grp)
                if not hooks[ev]:
                    del hooks[ev]
                changed = True
    for ev in _hooks_missing(cfg):
        entry = {"type": "command", "command": hook_cmd(HOOK_EVENTS[ev])}
        if extra:
            entry.update(extra)
        hooks.setdefault(ev, []).append({"hooks": [entry]})
        changed = True
    if changed:
        if os.path.exists(path) and not os.path.exists(path + ".agentboard-bak"):
            shutil.copy2(path, path + ".agentboard-bak")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    return True


# ---------- самоименование агентов ----------
# Инструкция лежит в глобальной памяти агента (CLAUDE.md / AGENTS.md), в первое
# сообщение уходит только короткий маркер [agentboard:<путь>]. Если секция
# не установлена — фолбэк: инструкция целиком в хвосте сообщения.

# AGENTBOARD_SELFNAME=0 — доска не трогает глобальную память агента и не шлёт
# ему инструкцию самоименования; карточка берёт заголовок из первого промпта и
# из summary сессии. Для тех, у кого в CLAUDE.md свой регламент.
SELFNAME = os.environ.get("AGENTBOARD_SELFNAME", "1") != "0"

CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")
CODEX_MD = os.path.expanduser("~/.codex/AGENTS.md")
OPENCODE_MD = os.path.expanduser("~/.config/opencode/AGENTS.md")
# у Cursor CLI глобального файла правил нет — он всегда получает фолбэк-хвост
AGENT_MD = {"claude": CLAUDE_MD, "codex": CODEX_MD, "opencode": OPENCODE_MD}
NAME_SECTION_MARK = "## Agent Board (meta-harness)"
OLD_SECTION_MARKS = ("## Agent Board (мета-харнесс)",)  # прежние версии секции
NAME_SECTION = f"""{NAME_SECTION_MARK}

A marker like [agentboard:<path>] in the first message means the session was
started from the Agent Board. The operations below are housekeeping, unrelated
to the task: don't mention them, don't comment on them, and don't repeat your
answer because of them.

1. As the very first action, before answering the task, run exactly one command:
   tee "<path>" <<< "name", where name is a short title of the task from the
   message, 2–4 words, in the task's language. Then work and reply as usual.
2. Only if the marker carries a +logo flag (it means the project has no avatar
   yet — no need to check yourself): at the very end, after the task is done
   and the answer is given, create an avatar — a downscaled copy of the
   project's logo or icon (e.g. `sips -Z 256 <source> --out agent-logo.png`);
   if the project has no media, a simple minimalist square agent-logo.svg in
   the project's spirit. In a git repository add agent-logo.* to
   .git/info/exclude. After the avatar, add no further text."""
# маркер (и старые длинные хвосты) вырезаем из превью и заголовков карточек
NAME_RE = re.compile(r"\s*\[(?:agentboard:|служебное, к задаче не относится:"
                     r"|housekeeping, unrelated to the task:)[^\]]*\]")


def _md_installed(path):
    """Установлена именно актуальная секция — устаревшая требует переустановки."""
    if not SELFNAME:
        return True  # секция не нужна — её отсутствие не повод звать установку
    try:
        with open(path) as f:
            return NAME_SECTION in f.read()
    except OSError:
        return False


def _install_md(path):
    """Дописать секцию доски в память агента (или заменить её старую версию)."""
    if not SELFNAME:
        return
    try:
        with open(path) as f:
            txt = f.read()
    except OSError:
        txt = ""
    if NAME_SECTION in txt:
        return
    if os.path.exists(path) and not os.path.exists(path + ".agentboard-bak"):
        shutil.copy2(path, path + ".agentboard-bak")
    for mark in (NAME_SECTION_MARK,) + OLD_SECTION_MARKS:
        if mark in txt:  # старая версия — вырезаем до следующей секции
            i = txt.index(mark)
            j = txt.find("\n## ", i)
            txt = (txt[:i].rstrip() + (txt[j:] if j != -1 else "\n")).lstrip("\n")
    txt = (txt.rstrip() + "\n\n" if txt.strip() else "") + NAME_SECTION + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(txt)


# ---- Warp: вкладка вместо окна ----
# launch configuration (см. _open_in_warp) всегда рождает окно, а вкладку умеет
# только warp://action/new_tab — но команду в неё не передать. Поэтому команду
# кладём в файл, а забирает её сам шелл новой вкладки: снипет ниже живёт в
# ~/.zshrc, срабатывает только в Warp, вне tmux и только на свежий файл
# (иначе случайная вкладка через час подхватила бы забытый attach).
#
# Attach нельзя делать на месте, прямо в ~/.zshrc: exec убивает zsh до того,
# как Warp допишет свой бутстрап, вкладка навсегда остаётся «инициализируется»
# и ввод не доходит до tmux (клавиши остаются в редакторе блока Warp). Поэтому
# ждём холостого хода ZLE (zsh/sched) — к этому моменту бутстрап дописан, — и
# перед exec сами дёргаем preexec-хуки: из них Warp узнаёт, что пошла команда,
# и отдаёт ей клавиатуру.

# AGENTBOARD_WARP_TABS=0 — не трогать ~/.zshrc и открывать окнами, как раньше
WARP_TABS = os.environ.get("AGENTBOARD_WARP_TABS", "1") != "0"
ZSHRC = os.path.expanduser("~/.zshrc")
WARP_MARK = "# >>> agentboard: Warp tabs >>>"
WARP_MARK_END = "# <<< agentboard: Warp tabs <<<"
WARP_ZSHRC = f"""{WARP_MARK}
if [[ -o interactive && "$TERM_PROGRAM" == "WarpTerminal" && -z "$TMUX" \
&& -f {WARP_ATTACH_FILE} ]]; then
  _agentboard_cmd=$(<{WARP_ATTACH_FILE})
  _agentboard_age=$(( $(date +%s) - $(stat -f %m {WARP_ATTACH_FILE}) ))
  rm -f {WARP_ATTACH_FILE}
  if [[ -n "$_agentboard_cmd" && $_agentboard_age -lt 30 ]]; then
    _agentboard_go() {{
      local cmd=$_agentboard_cmd hook
      unset _agentboard_cmd
      [[ -n "$cmd" ]] || return
      # Warp следит за командами через preexec — без этого сигнала он думает,
      # что шелл стоит на промпте, и клавиши до tmux не доходят
      for hook in ${{preexec_functions[@]}}; do
        "$hook" "$cmd" "$cmd" "$cmd" 2>/dev/null
      done
      eval "exec $cmd"
    }}
    if zmodload zsh/sched 2>/dev/null; then
      sched +1 _agentboard_go     # холостой ход ZLE: бутстрап Warp уже дописан
    else
      _agentboard_go              # без sched — хотя бы старым способом
    fi
  fi
  unset _agentboard_age
fi
{WARP_MARK_END}"""


def _warp_zshrc_ok():
    if not WARP_TABS:
        return False
    try:
        with open(ZSHRC) as f:
            return WARP_ZSHRC in f.read()
    except OSError:
        return False


def _install_warp_zshrc():
    """Дописать снипет в ~/.zshrc (старую версию блока — заменить)."""
    try:
        with open(ZSHRC) as f:
            txt = f.read()
    except OSError:
        txt = ""
    if WARP_ZSHRC in txt:
        return
    if os.path.exists(ZSHRC) and not os.path.exists(ZSHRC + ".agentboard-bak"):
        shutil.copy2(ZSHRC, ZSHRC + ".agentboard-bak")
    if WARP_MARK in txt:  # старая версия блока — вырезаем от метки до метки
        i = txt.index(WARP_MARK)
        j = txt.find(WARP_MARK_END, i)
        txt = txt[:i].rstrip() + (txt[j + len(WARP_MARK_END):] if j != -1 else "\n")
    txt = (txt.rstrip() + "\n\n" if txt.strip() else "") + WARP_ZSHRC + "\n"
    with open(ZSHRC, "w") as f:
        f.write(txt)


AGENT_BINS = {"claude": CLAUDE, "codex": CODEX, "cursor": CURSOR, "opencode": OPENCODE}


def detected_agents():
    return [a for a, b in AGENT_BINS.items() if os.path.exists(b)]


def install_hooks(selected=None):
    """Хуки и память — только выбранным провайдерам (по умолчанию всем найденным)."""
    sel = set(selected if selected is not None else detected_agents())
    sel &= set(detected_agents())  # не мусорим в конфигах неустановленных CLI
    if "claude" in sel:
        # async: хук-маячок не должен задерживать Claude
        _install_into(CLAUDE_SETTINGS, extra={"async": True})
        _install_md(CLAUDE_MD)
    if "codex" in sel:  # Codex поле async не знает — без него
        _install_into(CODEX_HOOKS_FILE)
        _install_md(CODEX_MD)
    if "cursor" in sel:
        _install_cursor()
    if "opencode" in sel:
        _install_opencode()
        _install_md(OPENCODE_MD)
    if TERMINAL_APP == "Warp" and WARP_TABS:  # без снипета Warp открывает окнами
        _install_warp_zshrc()
    return hooks_state()


# ---------- board.json: всё, что на доске ----------

# board.json читают и пишут несколько потоков — все read-modify-write под замком
BOARD_LOCK = threading.RLock()


def locked(fn):
    def wrapper(*args, **kwargs):
        with BOARD_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def load_board():
    """board.json: {"workspaces": [...], "cards": [...], "hidden": [проекты]}"""
    try:
        with open(BOARD_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    if isinstance(data, list):  # старый формат — только карточки
        data = {"cards": data}
    data.setdefault("workspaces", [])
    data.setdefault("cards", [])
    data.setdefault("hidden", [])
    data.setdefault("labels", {})  # id разговора -> имя; живёт дольше карточки
    data.setdefault("closed", [])  # недавно убранные с доски — можно вернуть
    data.setdefault("providers", None)  # None = онбординг ещё не пройден
    data.setdefault("models", {})  # agent -> {"favs": [id...], "def": id}
    return data


def save_board(board):
    with open(BOARD_FILE, "w") as f:
        json.dump(board, f, ensure_ascii=False, indent=1)


def sanitize(cwd):
    """Путь проекта -> имя папки в ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def session_file(cwd, sid):
    return os.path.join(PROJECTS_DIR, sanitize(cwd), sid + ".jsonl")


_sid_paths = {}  # sid -> найденный путь транскрипта


def find_session_file(cwd, sid):
    """Транскрипт по id: сперва папка проекта; если разговор резюмили из другой
    папки — файл остаётся в исходной, ищем по всем проектам."""
    p = session_file(cwd, sid)
    if os.path.isfile(p):
        return p
    hit = _sid_paths.get(sid)
    if hit and os.path.isfile(hit):
        return hit
    for p in glob.glob(os.path.join(PROJECTS_DIR, "*", sid + ".jsonl")):
        _sid_paths[sid] = p
        return p
    return ""


# служебный файл проекта — иконка агента на доске (как CLAUDE.md, только для борда)
LOGO_NAMES = ("agent-logo.png", "agent-logo.jpg", "agent-logo.jpeg",
              "agent-logo.webp", "agent-logo.svg")
LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".svg": "image/svg+xml"}


def find_logo(cwd):
    for n in LOGO_NAMES:
        p = os.path.join(cwd, n)
        if os.path.isfile(p):
            return p
    return None


def logo_version(cwd):
    p = find_logo(cwd)
    try:
        return int(os.stat(p).st_mtime) if p else 0
    except OSError:
        return 0


def session_records():
    """Живые регистрации Claude Code: pid -> sessionId (~/.claude/sessions)."""
    recs = []
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(p) as f:
                recs.append(json.load(f))
        except Exception:
            pass
    return recs


bg_cmd_cache = {}  # pid -> командная строка (распознавание bg-форков)


def bg_fork_of(rec, sid):
    """Фоновый форк (Agent View / remote control), продолжающий разговор sid.
    Демон клода запускает его как `claude --fork-session --resume <файл sid>`;
    записи в самом форке переписаны на новый sid, так что родословная видна
    только в командной строке живого процесса."""
    if rec.get("kind") != "bg" or not sid:
        return False
    pid = str(rec.get("pid", ""))
    cmd = bg_cmd_cache.get(pid)
    if cmd is None:
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            cmd = ""
        bg_cmd_cache[pid] = cmd
    return "--fork-session" in cmd and sid in cmd


def pane_pids(name):
    """PID процессов внутри tmux-сессии (панель + два уровня детей)."""
    pids = [p.strip() for p in
            tmux("list-panes", "-t", name, "-F", "#{pane_pid}").split() if p.strip()]
    found = set(pids)
    for _ in range(2):
        kids = []
        for pid in pids:
            try:
                r = subprocess.run(["pgrep", "-P", pid],
                                   capture_output=True, text=True, timeout=3)
                kids += r.stdout.split()
            except Exception:
                pass
        found.update(kids)
        pids = kids
    return found


# ---------- сессии, запущенные мимо доски ----------
# Клод сам ведёт реестр живых сессий: ~/.claude/sessions/<pid>.json с cwd,
# sessionId и статусом. Оттуда берём агентов, которых пользователь поднял в
# обычном терминале, — доска показывает их наравне со своими, только смотреть:
# tmux-сессии у них нет, значит ни attach, ни отправки текста.

EXT_PREFIX = "ext-"  # префикс имени карточки; tmux-сессии так называться не могут
proc_start_cache = {}  # pid -> время старта по данным ps


def _proc_start(pid):
    """Момент старта процесса, эпоха. Сравнивать строки нельзя: реестр пишет
    procStart в UTC, а ps печатает локальное время — расходятся на смещение."""
    if pid in proc_start_cache:
        return proc_start_cache[pid]
    stamp = 0.0
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
        stamp = time.mktime(time.strptime(" ".join(r.stdout.split()),
                                          "%a %b %d %H:%M:%S %Y"))
    except Exception:
        pass
    proc_start_cache[pid] = stamp
    return stamp


host_app_cache = {}  # pid -> человекочитаемое имя приложения-хозяина
APP_RE = re.compile(r"/([^/]+)\.app/")
APP_SHORT = {"Visual Studio Code": "VS Code"}  # длинное имя не влезает в плитку


def _host_app(pid):
    """Где живёт сессия: Warp, Cursor, iTerm… Ищем ближайшее .app вверх по
    родителям — сам claude всегда просто «claude code», а вот его предок
    (терминал или редактор) себя называет. Юзеру нужно знать, куда идти."""
    if pid in host_app_cache:
        return host_app_cache[pid]
    name, cur = "", pid
    try:
        for _ in range(5):
            r = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(cur)],
                               capture_output=True, text=True, timeout=5)
            parts = r.stdout.strip().split(None, 1)
            if len(parts) < 2:
                break
            # сперва смотрим на сам процесс: у launchd-детей (Cursor.app,
            # Warp.app) ppid уже 1, и проверка на корень съедала бы ответ
            m = APP_RE.search(parts[1])
            if m:
                name = m.group(1).split(" Helper")[0]
                name = APP_SHORT.get(name, name)
                break
            if parts[0] in ("0", "1"):
                break
            cur = parts[0]
    except Exception:
        pass
    host_app_cache[pid] = name
    return name


def _pid_gone(pid):
    """Процесса больше нет. Зомби считаем мёртвым: он ещё в таблице процессов
    (и os.kill по нему проходит), но уже ничего не исполняет и транскрипт
    не держит — ждать его дальше нечего."""
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    try:
        r = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip().startswith("Z")
    except Exception:
        return False


def _rec_alive(rec):
    """Запись реестра описывает живой процесс, а не переиспользованный pid."""
    pid = rec.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        proc_start_cache.pop(pid, None)
        return False
    # регистрация идёт сразу за стартом процесса; разъехались на минуты —
    # значит pid переиспользован и за записью стоит кто-то другой
    started = (rec.get("startedAt") or 0) / 1000
    real = _proc_start(pid)
    return not (started and real) or abs(started - real) < 300


def external_agents(busy_pids):
    """Живые сессии клода вне tmux — карточками того же вида, что и tmux-агенты.

    busy_pids — всё, что уже крутится внутри tmux-сессий: их доска показывает
    обычным путём, с превью панели и управлением."""
    agents = []
    now = time.time()
    for rec in session_records():
        if rec.get("kind") != "interactive" or str(rec.get("pid")) in busy_pids:
            continue
        cwd, sid = rec.get("cwd") or "", rec.get("sessionId") or ""
        if not cwd or not sid or not _rec_alive(rec):
            continue
        # хук ставит waiting по событию разрешения; сам реестр про вопрос
        # не знает, у него только busy/idle
        hook, hook_at = hook_status(EXT_PREFIX + sid)
        if hook == "waiting":
            status = "waiting"
        elif rec.get("status") == "busy":
            status = "working"
        else:
            status = "idle"
        _, title = cached_meta(find_session_file(cwd, sid))
        agents.append({
            "name": EXT_PREFIX + sid,
            "project": os.path.basename(cwd.rstrip("/")) or sid[:8],
            "path": cwd,
            "attached": False,
            "created": int((rec.get("startedAt") or 0) / 1000),
            "status": status,
            "preview": title or rec.get("name") or "",
            "activity": int((rec.get("updatedAt") or 0) / 1000) or int(now),
            "hook_at": hook_at if hook == "waiting" else 0,
            "external": True,
            "host": _host_app(rec["pid"]),
        })
    return agents


# ---------- история разговоров Claude Code ----------

def session_meta(path):
    """cwd и заголовок разговора из первых строк jsonl."""
    cwd, title = "", ""
    try:
        with open(path, errors="ignore") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not cwd and rec.get("cwd"):
                    cwd = rec["cwd"]
                if not title and rec.get("type") == "summary":
                    title = rec.get("summary", "")
                if not title and rec.get("type") == "user":
                    c = rec.get("message", {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    if isinstance(c, str):
                        c = c.strip()
                        if c and not c.startswith("<") and not c.startswith("Caveat"):
                            title = NAME_RE.sub("", c).strip()
                if cwd and title:
                    break
    except OSError:
        pass
    return cwd, " ".join(title.split())[:90]


_names = {"t": 0.0, "map": {}}


def session_names():
    """sessionId -> имя, которое юзер дал через /rename (~/.claude/sessions)."""
    now = time.time()
    if now - _names["t"] < 5:
        return _names["map"]
    best, m = {}, {}
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(p) as f:
                rec = json.load(f)
        except Exception:
            continue
        sid, name = rec.get("sessionId"), rec.get("name")
        if not sid or not name:
            continue
        u = rec.get("updatedAt", 0)
        if u >= best.get(sid, -1):
            best[sid], m[sid] = u, name
    _names["t"], _names["map"] = now, m
    return m


def cached_meta(path):
    try:
        m = os.stat(path).st_mtime
    except OSError:
        return "", ""
    hit = meta_cache.get(path)
    if hit and hit[0] == m:
        return hit[1], hit[2]
    cwd, title = session_meta(path)
    meta_cache[path] = (m, cwd, title)
    return cwd, title


def newest_session(cwd, after=0, exclude=()):
    """Свежайший разговор проекта: по времени последнего сообщения после момента
    after. Не по mtime файла — клод дописывает сервисные записи (ai-title,
    bridge-session, last-prompt) в старые файлы и делает мёртвые «свежими»."""
    best, best_m = None, 0
    for p in glob.glob(os.path.join(PROJECTS_DIR, sanitize(cwd), "*.jsonl")):
        if os.path.basename(p)[:-6] in exclude:
            continue
        m = log_activity(p, ("user", "assistant"))
        if m >= after - 60 and m > best_m:
            best, best_m = p, m
    if not best:
        return "", ""
    _, title = cached_meta(best)
    return os.path.basename(best)[:-6], title


def get_history(cwd=None, days=30, limit=50):
    """Разговоры, которые можно вытащить на доску."""
    pattern = os.path.join(PROJECTS_DIR, sanitize(cwd) if cwd else "*", "*.jsonl")
    now = time.time()
    files = []
    for p in glob.glob(pattern):
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
        if now - m <= days * 86400:
            files.append((m, p))
    files.sort(reverse=True)
    names = session_names()
    labels = load_board()["labels"]  # имена с доски главнее имён из /rename
    hist = []
    for m, p in files[:limit]:
        scwd, title = cached_meta(p)
        if not scwd:
            continue
        sid = os.path.basename(p)[:-6]
        hist.append({
            "id": sid,
            "cwd": scwd,
            "project": os.path.basename(scwd.rstrip("/")) or scwd,
            "name": labels.get(sid) or names.get(sid, ""),
            "title": title or "untitled",
            "age": int(now - m),
        })
    return hist


preview_cache = {}  # путь jsonl -> (mtime, превью)


def turn_preview(cwd, sid, include_trailing=True):
    """Последний тур из jsonl-транскрипта: полный текст, без терминального хрома.
    Якоримся на последнем ОТВЕЧЕННОМ вопросе: неотвеченный (прерванный) вопрос
    показываем хвостом, а не пустым туром."""
    path = find_session_file(cwd, sid)
    if not path:
        return ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = preview_cache.get(path)
    if hit and hit[0] == mtime and len(hit) == 3:
        return hit[1] if include_trailing else hit[2]
    try:
        size = os.path.getsize(path)
        with open(path, errors="ignore") as f:
            if size > 2_000_000:
                f.seek(size - 2_000_000)
                f.readline()  # добить обрезанную строку
            lines = f.readlines()
    except OSError:
        return ""

    def user_text(c):
        # юзер-контент бывает строкой или списком блоков
        if isinstance(c, str):
            s = c.strip()
        elif isinstance(c, list):
            s = " ".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text").strip()
        else:
            return ""
        s = NAME_RE.sub("", s).strip()
        return "" if not s or s.startswith("<") else s

    def tool_sig(item):
        # "Bash(tee /tmp/…)" — как в TUI; фронт красит строки "⏺ Имя(…)"
        name = item.get("name", "tool")
        parts = name.split("__", 2)
        if name.startswith("mcp__") and len(parts) == 3:
            # mcp__claude_ai_Pushkin__list_files -> "Pushkin: list_files"
            name = f"{parts[1].split('_')[-1]}: {parts[2]}"
        inp = item.get("input") or {}
        detail = (inp.get("command") or inp.get("file_path") or inp.get("path")
                  or inp.get("pattern") or inp.get("description") or "")
        detail = " ".join(str(detail).split())[:60]
        return f'{name}({detail or "…"})'

    items = []  # ("u"|"a"|"t", текст)
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("type") == "user":
            if r.get("isMeta"):
                continue  # инжекты харнесса (скиллы, преамбулы) — не сообщения юзера
            t = user_text(r.get("message", {}).get("content"))
            if t:
                items.append(("u", t))
        elif r.get("type") == "assistant":
            for item in r.get("message", {}).get("content", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text", "").strip():
                    items.append(("a", item["text"].strip()))
                elif item.get("type") == "tool_use":
                    items.append(("t", tool_sig(item)))
    if not items:
        preview_cache[path] = (mtime, "", "")
        return ""
    def render(chunk):
        out = []
        for kind, text in chunk:
            if kind == "u":
                out.append("> " + " ".join(text.split())[:500])
            elif kind == "t":
                out.append("⏺ " + text)
            else:
                out.append(text)
        t = "\n\n".join(out)
        return "\n".join(t.splitlines()[-500:])

    # занятый агент: якорь — твоё последнее сообщение (виден вопрос + растущий ответ)
    last_u = max((i for i, (k, _) in enumerate(items) if k == "u"), default=0)
    busy_text = render(items[last_u:])

    # тихий агент: последний ЗАВЕРШЁННЫЙ тур; мёртвые неотвеченные хвосты не показываем
    answered = None
    for i, (kind, _) in enumerate(items):
        if kind == "u" and any(k != "u" for k, _ in items[i + 1:]):
            answered = i
    seq = items[answered:] if answered is not None else items[last_u:]
    cut = len(seq)
    while cut > 0 and seq[cut - 1][0] == "u":
        cut -= 1
    idle_text = render(seq[:cut]) or busy_text

    preview_cache[path] = (mtime, busy_text, idle_text)
    return busy_text if include_trailing else idle_text


CODEX_SESS = os.path.expanduser("~/.codex/sessions")
codex_meta_cache = {}  # путь rollout -> (cwd, fork) из session_meta


def codex_meta(path):
    """cwd + признак саб-агентского форка (codex ≥0.144 пишет рядом rollout'ы
    subagent-тредов с тем же cwd — карточке они не принадлежат)."""
    if path in codex_meta_cache:
        return codex_meta_cache[path]
    cwd, fork = "", False
    try:
        with open(path, errors="ignore") as f:
            payload = json.loads(f.readline()).get("payload", {})
        cwd = payload.get("cwd", "")
        fork = bool(payload.get("parent_thread_id")) \
            or payload.get("thread_source") == "subagent"
    except Exception:
        pass
    codex_meta_cache[path] = (cwd, fork)
    return cwd, fork


def codex_id(ro):
    """Id разговора codex — uuid в имени rollout-файла (им же берёт `codex resume`)."""
    m = re.search(r"([0-9a-f-]{36})\.jsonl$", ro)
    return m.group(1) if m else ""


def codex_rollout(cwd, created, exclude=()):
    """Свежайший rollout codex для этой папки, начатый после старта tmux-сессии."""
    best, best_m = "", 0.0
    for path in glob.glob(os.path.join(CODEX_SESS, "*", "*", "*", "*.jsonl")):
        if path in exclude:
            continue
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if m < created - 60 or m <= best_m:
            continue
        p_cwd, fork = codex_meta(path)
        if p_cwd == cwd and not fork:
            best, best_m = path, m
    return best


def codex_turn_preview(path):
    """Последний тур из rollout-файла codex."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = preview_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        size = os.path.getsize(path)
        with open(path, errors="ignore") as f:
            if size > 400_000:
                f.seek(size - 400_000)
                f.readline()
            lines = f.readlines()
    except OSError:
        return ""
    items = []
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("type") != "response_item":
            continue
        pay = r.get("payload", {})
        if pay.get("type") == "message":
            text = " ".join(seg.get("text", "") for seg in pay.get("content", [])
                            if isinstance(seg, dict) and seg.get("text"))
            items.append((pay.get("role"), text))
        elif pay.get("type") in ("function_call", "custom_tool_call",
                                 "local_shell_call", "web_search_call"):
            name = pay.get("name") or pay.get("type").replace("_call", "")
            detail = ""
            if pay.get("type") == "function_call":
                try:
                    args = json.loads(pay.get("arguments") or "{}")
                    detail = args.get("cmd") or args.get("command") or ""
                except ValueError:
                    pass
            elif pay.get("type") == "custom_tool_call":
                # команда зашита в JS-обёртку: tools.exec_command({cmd:"…"})
                m = re.search(r'"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"', pay.get("input") or "")
                if m:
                    try:
                        detail = json.loads('"' + m.group(1) + '"')
                    except ValueError:
                        pass
            elif pay.get("type") == "local_shell_call":
                detail = (pay.get("action") or {}).get("command") or ""
            if isinstance(detail, list):
                detail = " ".join(detail)
            detail = " ".join(str(detail).split())[:60]
            items.append(("tool", f"{name}({detail or '…'})"))
    start = None
    for i, (role, text) in enumerate(items):
        # scaffold-сообщения codex ≥0.144 идут с ролью user — они не реплика
        if role == "user" and text.strip() \
                and not text.lstrip().startswith(("<", "# AGENTS.md")):
            start = i
    if start is None:
        # длинный тур: реплика юзера уехала за 400КБ-окно — показываем хвост работы
        out = []
        tail = items
    else:
        out = ["> " + " ".join(NAME_RE.sub("", items[start][1]).split())[:500]]
        tail = items[start + 1:]
    for role, text in tail:
        if role == "assistant" and text.strip():
            out.append(text.strip())
        elif role == "tool":
            out.append("⏺ " + text)
    if not out:
        preview_cache[path] = (mtime, "")
        return ""
    text = "\n\n".join(out)
    text = "\n".join(text.splitlines()[-500:])
    preview_cache[path] = (mtime, text)
    return text


# ---------- сессии Cursor: ~/.cursor/chats/<md5(cwd)>/<uuid>/store.db ----------

CURSOR_CHATS = os.path.expanduser("~/.cursor/chats")
cursor_model_cache = {}  # путь store.db -> имя модели из блобов


def _cursor_meta(db):
    try:
        with open(os.path.join(os.path.dirname(db), "meta.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cursor_chat(cwd, created, exclude=()):
    """Свежайший чат Cursor этой папки, тронутый после старта tmux-сессии."""
    h = hashlib.md5(cwd.encode()).hexdigest()
    best, best_t = "", 0
    for db in glob.glob(os.path.join(CURSOR_CHATS, h, "*", "store.db")):
        if db in exclude:
            continue
        t = _cursor_meta(db).get("updatedAtMs", 0)
        if t < (created - 60) * 1000 or t <= best_t:
            continue
        best, best_t = db, t
    return best


def cursor_turn_preview(db):
    """Последний тур из store.db: JSON-блобы сообщений в порядке вставки."""
    stamp = _cursor_meta(db).get("updatedAtMs", 0)  # mtime базы не годится (WAL)
    hit = preview_cache.get(db)
    if hit and hit[0] == stamp:
        return hit[1]
    items = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        con.text_factory = bytes  # среди блобов есть protobuf — не декодируется
        rows = con.execute("SELECT data FROM blobs ORDER BY rowid").fetchall()
        con.close()
    except sqlite3.Error:
        return ""
    for (raw,) in rows:
        if not raw or not raw.startswith(b'{"role":'):
            continue
        try:
            r = json.loads(raw.decode("utf-8", "ignore"))
        except ValueError:
            continue
        content = r.get("content")
        if r.get("role") == "user":
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
            m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
            text = m.group(1) if m else text.strip()
            text = NAME_RE.sub("", text).strip()
            if text and not text.startswith("<"):
                items.append(("user", text))
        elif r.get("role") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                model = (b.get("providerOptions") or {}).get("cursor", {}).get("modelName")
                if model:
                    cursor_model_cache[db] = model
                if b.get("type") == "text" and b.get("text", "").strip():
                    items.append(("assistant", b["text"].strip()))
                elif b.get("type") in ("tool-call", "tool_call"):
                    args = b.get("args") or b.get("input") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    detail = (args.get("command") or args.get("cmd")
                              or args.get("file_path") or args.get("path")
                              or args.get("pattern") or args.get("query") or "")
                    detail = " ".join(str(detail).split())[:60]
                    name = b.get("toolName") or b.get("name") or "tool"
                    items.append(("tool", f"{name}({detail or '…'})"))
    start = max((i for i, (k, _) in enumerate(items) if k == "user"), default=None)
    if start is None:
        preview_cache[db] = (stamp, "")
        return ""
    out = ["> " + " ".join(items[start][1].split())[:500]]
    for kind, text in items[start + 1:]:
        out.append("⏺ " + text if kind == "tool" else text)
    text = "\n".join("\n\n".join(out).splitlines()[-500:])
    preview_cache[db] = (stamp, text)
    return text


# ---------- сессии opencode: общий sqlite ~/.local/share/opencode ----------

OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")


def _opencode_q(sql, args=()):
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(sql, args).fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        return []


def opencode_session(cwd, created, exclude=()):
    """id свежайшей сессии opencode этой папки после старта tmux-сессии."""
    for (sid,) in _opencode_q(
            "SELECT id FROM session WHERE directory = ? AND time_updated >= ? "
            "ORDER BY time_updated DESC", (cwd, int((created - 60) * 1000))):
        if sid not in exclude:
            return sid
    return ""


def opencode_meta(sid):
    """(activity, model) сессии — из её строки в базе."""
    rows = _opencode_q("SELECT time_updated, model FROM session WHERE id = ?", (sid,))
    if not rows:
        return 0, ""
    t, model = rows[0]
    if model and model.startswith("{"):  # модель хранится JSON-объектом
        try:
            m = json.loads(model)
            model = m.get("id") or m.get("modelID") or ""
        except ValueError:
            model = ""
    return int((t or 0) / 1000), model or ""


def opencode_turn_preview(sid):
    """Последний тур: message/part из общей базы, в хронологии."""
    stamp = opencode_meta(sid)[0]
    hit = preview_cache.get(sid)
    if hit and hit[0] == stamp:
        return hit[1]
    roles = dict(_opencode_q(
        "SELECT id, json_extract(data, '$.role') FROM message "
        "WHERE session_id = ?", (sid,)))
    items = []
    for mid, raw in _opencode_q(
            "SELECT message_id, CAST(data AS TEXT) FROM part "
            "WHERE session_id = ? ORDER BY time_created", (sid,)):
        try:
            p = json.loads(raw)
        except ValueError:
            continue
        role = roles.get(mid, "")
        if p.get("type") == "text" and p.get("text", "").strip():
            text = p["text"].strip()
            if role == "user":
                text = NAME_RE.sub("", text).strip()
                if not text or text.startswith("<"):
                    continue
            items.append((role, text))
        elif p.get("type") == "tool":
            inp = (p.get("state") or {}).get("input") or {}
            detail = (inp.get("command") or inp.get("cmd") or inp.get("filePath")
                      or inp.get("path") or inp.get("pattern") or "")
            detail = " ".join(str(detail).split())[:60]
            items.append(("tool", f"{p.get('tool', 'tool')}({detail or '…'})"))
    start = max((i for i, (k, _) in enumerate(items) if k == "user"), default=None)
    if start is None:
        preview_cache[sid] = (stamp, "")
        return ""
    out = ["> " + " ".join(items[start][1].split())[:500]]
    for kind, text in items[start + 1:]:
        out.append("⏺ " + text if kind == "tool" else text)
    text = "\n".join("\n\n".join(out).splitlines()[-500:])
    preview_cache[sid] = (stamp, text)
    return text


# ---------- общий интерфейс к структурным логам не-клодов ----------
# card["rollout"]: codex и cursor — путь к файлу лога, opencode — id сессии.

def find_log(agent, cwd, created, exclude):
    if agent == "codex":
        return codex_rollout(cwd, created, exclude)
    if agent == "cursor":
        return cursor_chat(cwd, created, exclude)
    if agent == "opencode":
        return opencode_session(cwd, created, exclude)
    return ""


def log_valid(agent, ro):
    if not ro:
        return False
    if agent == "opencode":
        return bool(_opencode_q("SELECT 1 FROM session WHERE id = ?", (ro,)))
    if agent == "codex":
        # привязанный когда-то форк саб-агента отпускаем — карточка перепривяжется
        return os.path.isfile(ro) and not codex_meta(ro)[1]
    return os.path.isfile(ro)


def log_stamp(agent, ro):
    if agent == "codex":
        return log_activity(ro)
    if agent == "cursor":
        return int(_cursor_meta(ro).get("updatedAtMs", 0) / 1000)
    return opencode_meta(ro)[0]


def log_model_of(agent, ro):
    if agent == "codex":
        return log_model(ro, "codex")
    if agent == "cursor":
        cursor_turn_preview(ro)  # модель добывается по пути разбора блобов
        return cursor_model_cache.get(ro, "")
    return opencode_meta(ro)[1]


def log_preview(agent, ro):
    if agent == "codex":
        return codex_turn_preview(ro)
    if agent == "cursor":
        return cursor_turn_preview(ro)
    return opencode_turn_preview(ro)


# ---------- живые агенты (tmux) ----------

def get_live():
    rows = tmux("list-sessions", "-F",
                "#{session_name}\t#{session_path}\t#{session_attached}\t#{session_created}")
    agents = []
    busy_pids = set()
    now = time.time()
    for line in rows.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, path, attached, created = parts
        busy_pids |= pane_pids(name)
        pane = tmux("capture-pane", "-p", "-t", name, "-S", "-2000")
        content = []
        tip_wrap = False  # Tip: переносится на несколько строк — режем весь абзац
        for raw in pane.splitlines():
            s = raw.replace(" ", " ").strip()
            if tip_wrap:
                if not s or s.startswith(("⏺", "⎿", ">", "✻", "✳", "❯")):
                    tip_wrap = False
                else:
                    continue
            if s.lstrip("⎿ ").startswith("Tip:"):
                tip_wrap = True
                continue
            if not is_chrome(raw):
                content.append(raw.rstrip())
        # последний тур целиком: от последнего сообщения юзера ("> ...") до конца
        start = 0
        for i, l in enumerate(content):
            if l.lstrip().startswith("> "):
                start = i
        preview = "\n".join(content[start:][-500:])

        h = hash(pane)
        rec = last_seen.setdefault(name, {"hash": h, "changed": now})
        hook, hook_at = hook_status(name)
        if rec["hash"] != h:
            # хук на отказ не срабатывает; распознаём ответ юзера в терминале:
            # диалог держит панель неподвижной, ответ её оживляет
            if hook == "waiting" and now - rec["changed"] > 4:
                rec["answered"] = hook_at
            rec["hash"], rec["changed"] = h, now
        if hook == "waiting" and rec.get("answered") == hook_at:
            hook = "working"  # на этот вопрос уже ответили руками
        if hook == "waiting":
            status = "waiting"
        elif now - rec["changed"] < 10:
            status = "working"
        elif hook == "working" and now - hook_at < 900 and now - rec["changed"] < 120:
            # хук сказал "работает", панель тихая — верим ещё 2 минуты. Но не
            # старому файлу: Stop из фоновых форков (Agent View) не приходит
            # (нет $TMUX), и «working» может висеть часами
            status = "working"
        else:
            status = "idle"

        agents.append({
            "name": name,
            "project": os.path.basename(path.rstrip("/")) or name,
            "path": path,
            "attached": attached != "0",
            "created": int(created or 0),
            "status": status,
            "preview": preview,
            "activity": int(created or 0),
            # когда «ждёт» сказал хук — момент вопроса; нужен ниже, чтобы
            # отлеплять протухший waiting по активности структурного лога
            "hook_at": hook_at if hook == "waiting" else 0,
        })
    agents += external_agents(busy_pids)
    update_caffeinate(any(a["status"] == "working" for a in agents))
    return agents


last_agents = None  # последний снимок: им отвечаем, пока идёт соседний скан


def get_agents():
    """Снимок для доски. В очередь за BOARD_LOCK не встаём — см. ниже."""
    global last_agents
    # доска опрашивает нас каждые 2 с, не дожидаясь предыдущего ответа, а скан
    # держит BOARD_LOCK всё время обхода сессий. Стоит одному проходу не уложиться
    # в 2 с — ожидающие начинают копиться, отбирают GIL у считающего, проход
    # тормозит ещё сильнее, и очередь растёт быстрее, чем разгребается. Из такой
    # лавины (наблюдали 571 поток) сервер сам уже не выходит. Поэтому: занят —
    # мгновенно отдаём прошлый снимок, он отстаёт на секунду и это незаметно.
    if not BOARD_LOCK.acquire(blocking=False):
        if last_agents is not None:
            return last_agents
        BOARD_LOCK.acquire()  # первый запрос после старта: отдавать нечего, ждём
    try:
        last_agents = _get_agents()
        return last_agents
    finally:
        BOARD_LOCK.release()


def _get_agents():
    """Живые из tmux + карточки на паузе. Всё живое само попадает в board.json."""
    live = get_live()
    board = load_board()
    cards_list = board["cards"]
    changed = False

    # доска, жившая до онбординга, — считаем, что выбраны все найденные CLI
    if board["providers"] is None and (cards_list or board["workspaces"]):
        board["providers"] = detected_agents()
        changed = True

    # дедупликация: один разговор — одна карточка. Из двух держим ту, у которой
    # живой терминал, а имя переносим: раньше выживала просто первая в списке —
    # самоназванная карточка молча теряла имя, а живая пересоздавалась каждый опрос
    live_names = {a["name"] for a in live}
    seen = {}
    for card in list(cards_list):
        key = card.get("id") or ("tmux:" + card.get("tmux", ""))
        first = seen.get(key)
        if first is None:
            seen[key] = card
            continue
        alive = card.get("tmux") in live_names and first.get("tmux") not in live_names
        keep, drop = (card, first) if alive else (first, card)
        keep["label"] = keep.get("label") or drop.get("label", "")
        if keep.get("id") and keep["label"]:
            board["labels"][keep["id"]] = keep["label"]
        seen[key] = keep
        cards_list.remove(drop)
        changed = True

    by_tmux = {c.get("tmux"): c for c in cards_list if c.get("tmux")}
    ws_projects = {w["project"] for w in board["workspaces"]}

    # имена от самих агентов: файл в NAMES_DIR, имя файла = tmux-сессия (см. name_tail)
    try:
        name_files = os.listdir(NAMES_DIR)
    except OSError:
        name_files = []
    for fn in name_files:
        p = os.path.join(NAMES_DIR, fn)
        card = by_tmux.get(fn)
        # ручное имя (с доски или по id) всегда главнее агентского
        if card and not (card.get("label") or board["labels"].get(card.get("id") or "")):
            try:
                with open(p) as f:
                    label = " ".join(f.read().split())[:60]
            except OSError:
                label = ""
            if label:
                card["label"] = label
                if card.get("id"):
                    board["labels"][card["id"]] = label
                changed = True
        try:
            os.remove(p)
        except OSError:
            pass

    # у каждого живого агента должен быть воркспейс (живой проект снимает скрытие)
    for a in live:
        if a["project"] in board["hidden"]:
            board["hidden"].remove(a["project"])
            changed = True
        if a["project"] not in ws_projects:
            board["workspaces"].append({"project": a["project"], "cwd": a["path"]})
            ws_projects.add(a["project"])
            changed = True

    recs = session_records()
    names = session_names()
    for a in live:
        card = by_tmux.get(a["name"])
        if not card:
            card = {"tmux": a["name"], "cwd": a["path"],
                    "project": a["project"], "id": "", "title": ""}
            cards_list.append(card)
            by_tmux[a["name"]] = card
            changed = True
        a["agent"] = card.get("agent", "claude")
        a["model"] = model_label(card.get("model", ""))
        if a["agent"] != "claude":
            # claude-сессии у не-клодов нет; id разговора есть только у codex (ниже)
            a["cid"] = ""
            a["sname"] = ""
            a["logo"] = logo_version(a["path"])
            ro = card.get("rollout", "")
            if not log_valid(a["agent"], ro):
                ro = find_log(a["agent"], card["cwd"], a["created"],
                              {c.get("rollout") for c in cards_list
                               if c is not card and c.get("rollout")})
                if ro:
                    card["rollout"] = ro
                    changed = True
            # codex умеет `codex resume <uuid>` — даём карточке id разговора,
            # на нём держатся имя, «недавно закрытые» и возобновление
            if a["agent"] == "codex" and ro:
                cid = codex_id(ro)
                if cid and cid != card.get("id"):
                    card["id"] = cid
                    changed = True
                a["cid"] = card.get("id", "")
            a["label"] = card.get("label") or board["labels"].get(a["cid"], "")
            # превью — только из структурного лога: в пейне на старте
            # прокручивается служебный шум (MCP, лимиты), в логе его нет
            pane_preview = a["preview"]
            a["preview"] = ""
            if ro:
                a["activity"] = log_stamp(a["agent"], ro) or a["activity"]
                found_model = log_model_of(a["agent"], ro)
                if found_model:
                    a["model"] = model_label(found_model)
                    if not card.get("model"):
                        card["model"] = found_model
                        changed = True
                a["preview"] = log_preview(a["agent"], ro)
                # протухший «ждёт»: лог живёт после вопроса хука — значит,
                # уже ответили (спиннер TUI не даёт пейн-эвристике отлепить)
                if a["status"] == "waiting" and a["hook_at"] \
                        and a["activity"] > a["hook_at"] + 5:
                    a["status"] = "working"
            elif a["agent"] == "codex" and time.time() - a["created"] > 15:
                # codex за 15 секунд не создал rollout — стоит на стартовом
                # диалоге (trust/hooks/login): де-факто «ждёт тебя», а не
                # «запускается». Сигнал структурный, пейн — только для показа.
                a["status"] = "waiting"
                lines = [l.strip() for l in pane_preview.splitlines() if l.strip()]
                a["preview"] = "\n".join(lines[-10:])
            continue
        if a.get("external"):
            # разговор известен из реестра клода: pane_pids звать нельзя
            # (tmux-сессии у карточки нет), да и угадывать нечего
            sid = a["name"][len(EXT_PREFIX):]
        else:
            # точная привязка: PID процесса claude внутри панели -> sessionId
            pids = pane_pids(a["name"])
            rec = next((r for r in recs
                        if str(r.get("pid")) in pids and r.get("sessionId")), None)
            sid = rec["sessionId"] if rec else card["id"]
            # компакшн/суммаризация форкают разговор в НОВЫЙ файл на ходу, а pid->session
            # у claude при этом не обновляется — карточка застывала на старом. Следуем за
            # более свежим разговором той же папки (не заглатывая сессии других живых
            # карточек и недавно закрытые). Активную сессию это не трогает: её файл и есть
            # самый свежий, так что подхватывается только осиротевший после форка.
            known = ({c["id"] for c in cards_list if c is not card and c.get("id")} |
                     {c["id"] for c in board["closed"] if c.get("id")})
            # id чужих карточек появляются с задержкой (свежая сессия ещё без id),
            # а pid-файлы пишутся мгновенно: сессию, на которую претендует любой
            # другой живой pid, не заглатываем. Исключение — bg-форк нашего же
            # разговора (Agent View): это не чужая карточка, а его продолжение.
            # Форков в pid-файлах не бывает (они там не обновляются — в этом и был
            # баг), так что форк подхватится.
            known |= {r["sessionId"] for r in recs
                      if r.get("sessionId") and str(r.get("pid")) not in pids
                      and not bg_fork_of(r, sid)}
            cand, _ = newest_session(a["path"], a["created"], known)
            if cand and cand != sid:
                cur_f = find_session_file(a["path"], sid) if sid else ""
                cur_m = log_activity(cur_f, ("user", "assistant")) if cur_f else 0
                if log_activity(find_session_file(a["path"], cand),
                                ("user", "assistant")) > cur_m:
                    sid = cand
        if sid and sid != card["id"]:
            card["id"] = sid
            _, card["title"] = cached_meta(find_session_file(card["cwd"], sid))
            changed = True
        a["cid"] = card["id"]
        a["sname"] = names.get(card["id"], "")
        a["label"] = card.get("label") or board["labels"].get(card["id"], "")
        a["logo"] = logo_version(a["path"])
        if card["id"]:
            session_path = find_session_file(card["cwd"], card["id"])
            # разговор переживает процесс: перехват из чужого терминала поднимает
            # новый CLI, но тот же лог — на карточке видно оба возраста
            a["started"] = log_started(session_path)
            a["activity"] = log_activity(session_path, ("user", "assistant")) or a["activity"]
            found_model = log_model(session_path, "claude")
            if found_model:
                a["model"] = model_label(found_model)
                if not card.get("model"):
                    card["model"] = found_model
                    changed = True
            try:  # кривой лог одной карточки не должен ронять всю доску
                tp = turn_preview(card["cwd"], card["id"],
                                  a["status"] in ("working", "waiting"))
            except Exception:
                tp = ""
            if tp:
                a["preview"] = tp

    # ---- «требует тебя»: то, что зажигает счётчик на иконке ----
    # Только диалог разрешения. Законченную работу сюда не считаем: агент,
    # который отработал и молчит, ничего от тебя не требует.
    for a in live:
        a["attention"] = a["status"] == "waiting"

    agents = live
    for card in list(cards_list):
        if card.get("tmux") in live_names:
            continue
        if card.get("tmux"):
            card["tmux"] = ""  # сессия умерла — отвязываем, чтобы имя не всплыло у чужой карточки
            changed = True
        if not card.get("id"):
            cards_list.remove(card)  # умерла, не успев поговорить — нечего возобновлять
            changed = True
            continue
        agent = card.get("agent", "claude")
        if agent == "claude":
            session_path = find_session_file(card["cwd"], card["id"])
            activity = log_activity(session_path, ("user", "assistant"))
            raw_model = card.get("model") or log_model(session_path, "claude")
        else:  # у не-клодов разговор живёт в своём логе (id есть только у codex)
            ro = card.get("rollout", "")
            activity = log_stamp(agent, ro) if ro else 0
            raw_model = card.get("model") or (log_model_of(agent, ro) if ro else "")
        if raw_model and not card.get("model"):
            card["model"] = raw_model
            changed = True
        agents.append({
            "name": "pause:" + card["id"],
            "cid": card["id"],
            "sname": names.get(card["id"], ""),
            "label": card.get("label") or board["labels"].get(card["id"], ""),
            "logo": logo_version(card["cwd"]),
            "project": card["project"],
            "path": card["cwd"],
            "attached": False,
            "agent": agent,
            "model": model_label(raw_model),
            "status": "parked",
            "preview": card["title"] or "untitled conversation",
            "activity": activity,
        })

    if changed:
        save_board(board)
    try:
        page = int(os.stat(os.path.join(HERE, "index.html")).st_mtime)
    except OSError:
        page = 0
    return {"workspaces": [dict(w, logo=logo_version(w["cwd"]))
                           for w in board["workspaces"]],
            "agents": agents, "page": page,
            "claude": os.path.exists(CLAUDE),
            "codex": os.path.exists(CODEX),
            "cursor": os.path.exists(CURSOR),
            "opencode": os.path.exists(OPENCODE),
            "providers": board["providers"],
            "models": board["models"],
            "version": __version__, "update": UPDATE["available"],
            "hooks": hooks_state(), "search": search_available()}


# ---------- встроенный терминал: WebSocket + PTY поверх `tmux attach` ----------
# Библиотеки не берём: рукопожатие — sha1+base64, фрейминг — десяток строк.
# Канал даёт исполнение команд, поэтому пускаем его через ту же проверку
# origin, что и остальные ручки (см. Handler.cross_origin).
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept(key):
    return base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()


def ws_frame(payload, opcode=0x2):
    """Кадр сервер→клиент: без маски, длина в одном из трёх форматов."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def ws_read(rfile):
    """Кадр клиент→сервер: маска обязательна. None — поток кончился.

    Фрагментацию (FIN=0) не собираем: браузер шлёт ввод терминала мелкими
    целыми кадрами, а огромных сообщений тут не бывает.
    """
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode, masked, n = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", rfile.read(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else b""
    data = rfile.read(n) or b""
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return opcode, data


def pty_resize(fd, cols, rows):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def term_attach(sock, rfile, name, cols, rows):
    """Гоняем байты между сокетом и `tmux attach -t name` в отдельном PTY.

    Свой PTY на вкладку, а не общий с доской: tmux размер берёт у последнего
    подключённого клиента, и общий терминал переклеил бы всем сессиям геометрию.
    """
    pid, fd = pty.fork()
    if pid == 0:  # ребёнок: он и есть терминал
        os.environ["TERM"] = "xterm-256color"
        os.environ["PATH"] = AGENT_PATH
        try:
            os.execv(TMUX_CMD[0], [*TMUX_CMD, "attach", "-t", name])
        finally:
            os._exit(1)
    pty_resize(fd, cols, rows)

    def pump():  # PTY → сокет
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                sock.sendall(ws_frame(chunk))
        except (OSError, ValueError):
            pass
        finally:
            try:
                sock.sendall(ws_frame(b"", 0x8))  # close
            except OSError:
                pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        while True:
            got = ws_read(rfile)
            if got is None:
                break
            opcode, data = got
            if opcode == 0x8:  # клиент закрыл вкладку
                break
            if opcode == 0x9:  # ping → pong
                sock.sendall(ws_frame(data, 0xA))
                continue
            if opcode == 0x1:
                # текстовый кадр — управление; ввод идёт бинарными, поэтому
                # «{» с клавиатуры не притворится командой
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if msg.get("t") == "size":
                    pty_resize(fd, int(msg.get("cols", cols)),
                               int(msg.get("rows", rows)))
                continue
            os.write(fd, data)
    except OSError:
        pass
    finally:
        # честно гасим: осиротевший attach держал бы PTY и поток вечно
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGHUP)
            os.waitpid(pid, 0)
        except OSError:
            pass


# что панель умеет запускать вкладкой. Список закрытый: команду с клиента не
# принимаем, иначе ручка превращается в «выполни что угодно»
TERM_APPS = ("mc", "nnn", "ranger", "yazi", "lf")


def new_shell(cwd="", app=""):
    """Оболочка (или файловый менеджер) вкладкой: не агент, карточку не заводим.

    Размер тот же 220×50, что и у агентов: панель всё равно ресайзит сессию под
    себя, а рождённая мелкой сессия потом мажет при первом же изменении.
    """
    cwd = cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~")
    binary = shutil.which(app) if app in TERM_APPS else ""
    run = "exec " + shlex.quote(binary) if binary else "exec $SHELL -l"
    base = ("fm-" if binary else "sh-") + str(int(time.time()) % 100000)
    name, i = base, 0
    while tmux_ok("has-session", "-t", name):
        i += 1
        name = base + "-" + str(i)
    tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", cwd,
         f"{LOCALE_EXPORT}; export PATH={shlex.quote(AGENT_PATH)}; {run}")
    tmux("set-option", "-t", name, "mouse", "on")
    tmux("set-option", "-t", name, "mode-style", "bg=colour236,fg=colour245")
    return {"session": name, "app": app if binary else "", "asked": app}


# ---------- действия ----------

# ---------- каталоги моделей: сами CLI + имена из models.dev ----------
# models.dev — открытый каталог, которым пользуется сам opencode: даёт
# человеческие имена («Kimi K3») для url-подобных id и имена провайдеров.

MODELSDEV_URL = "https://models.dev/api.json"
_modelsdev = {"t": 0.0, "data": {}}


def modelsdev():
    if _modelsdev["data"] and time.time() - _modelsdev["t"] < 86400:
        return _modelsdev["data"]
    try:
        req = urllib.request.Request(MODELSDEV_URL, headers={"User-Agent": "agentboard"})
        with urllib.request.urlopen(req, timeout=20) as r:
            _modelsdev["data"] = json.load(r)
            _modelsdev["t"] = time.time()
    except Exception:
        pass  # без сети остаёмся на голых id
    return _modelsdev["data"]


models_cache = {}  # agent -> (ts, список)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def agent_models(agent):
    """Каталог моделей агента: cursor/opencode спрашиваем у CLI, codex — по
    линейке openai в models.dev (своей команды списка у него нет). Кэш 10 мин."""
    hit = models_cache.get(agent)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = []
    try:
        if agent == "cursor":
            r = subprocess.run([CURSOR, "--list-models"],
                               capture_output=True, text=True, timeout=20)
            for line in ANSI_RE.sub("", r.stdout).splitlines():
                m = re.match(r"\s*(\S+)\s+-\s+(.+)", line)
                if not m:
                    continue
                mid, label = m.group(1), m.group(2).strip()
                default = "default" in label
                label = re.sub(r"\s*\((?:current|default)[^)]*\)", "", label).strip()
                out.append({"id": mid, "l": label or mid, "def": default})
        elif agent == "opencode":
            r = subprocess.run([OPENCODE, "models"],
                               capture_output=True, text=True, timeout=30)
            cfg, _ = _read_json(OPENCODE_CONFIG)
            default = cfg.get("model", "")
            cat = modelsdev()
            for mid in r.stdout.split():
                if "/" not in mid:
                    continue
                prov, _sep, rest = mid.partition("/")
                pcat = cat.get(prov) or {}
                name = (pcat.get("models", {}).get(rest) or {}).get("name")
                out.append({"id": mid, "l": name or rest,
                            "prov": pcat.get("name") or prov,
                            "def": mid == default})
        elif agent == "codex":
            ms = (modelsdev().get("openai") or {}).get("models", {})
            for mid, m in sorted(ms.items(), reverse=True):
                if "codex" in mid or mid.startswith("gpt-5"):
                    out.append({"id": mid, "l": m.get("name") or mid,
                                "def": mid == "gpt-5.6-sol"})
        elif agent == "claude":
            out = [{"id": "fable", "l": "Fable 5", "def": True},
                   {"id": "opus", "l": "Opus 4.8", "def": False},
                   {"id": "sonnet", "l": "Sonnet 5", "def": False},
                   {"id": "haiku", "l": "Haiku 4.5", "def": False}]
    except Exception:
        pass
    if out:
        models_cache[agent] = (time.time(), out)
    return out


# ---------- доверие к папке: гасим стартовый диалог «trust this directory?» ----------
# Без этого агент в незнакомой папке молча стоит на диалоге, а карточка
# выглядит «запускается». Создание агента с доски — и есть согласие юзера.
# Хранилища: claude — ~/.claude.json projects[cwd].hasTrustDialogAccepted;
# codex — [projects."cwd"] в config.toml; cursor — ~/.cursor/projects/<слаг>/
# .workspace-trusted (слаг = путь, не-алфанум → дефисы; cursor сверяет
# workspacePath, так что промах слага просто вернёт диалог, не сломает).

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
CURSOR_PROJECTS = os.path.expanduser("~/.cursor/projects")


def pre_trust(agent, cwd):
    try:
        if agent == "claude":
            cfg, ok = _read_json(CLAUDE_JSON)
            if not ok or not cfg:  # нет файла — claude ещё не запускали, не лезем
                return
            proj = cfg.setdefault("projects", {}).setdefault(cwd, {})
            if not proj.get("hasTrustDialogAccepted"):
                proj["hasTrustDialogAccepted"] = True
                tmp = CLAUDE_JSON + ".agentboard-tmp"
                with open(tmp, "w") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CLAUDE_JSON)
        elif agent == "codex":
            mark = f'[projects."{cwd}"]'
            try:
                with open(CODEX_CONFIG) as f:
                    txt = f.read()
            except OSError:
                txt = ""
            if mark not in txt:
                os.makedirs(os.path.dirname(CODEX_CONFIG), exist_ok=True)
                with open(CODEX_CONFIG, "a") as f:
                    f.write(f'\n{mark}\ntrust_level = "trusted"\n')
        elif agent == "cursor":
            slug = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]", "-", cwd)).strip("-")
            p = os.path.join(CURSOR_PROJECTS, slug, ".workspace-trusted")
            if not os.path.exists(p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    json.dump({"trustedAt":
                               datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                               "workspacePath": cwd}, f, indent=2)
    except Exception:
        pass  # не вышло — агент просто спросит сам, как раньше


def free_name(base):
    name, i = base, 2
    while tmux_ok("has-session", "-t", name):
        name = f"{base}-{i}"
        i += 1
    return name


def _open_in_warp(name, attached):
    """Warp не открывает .command и не скриптуется — зато умеет launch
    configuration: YAML в ~/.warp/launch_configurations и переход по
    warp://launch/<имя>. Точной вкладки у нас нет, поэтому уже подключённую
    сессию просто выносим вперёд вместе с приложением.

    Launch configuration всегда открывает новое окно. Если в ~/.zshrc стоит
    снипет доски (WARP_ZSHRC) — идём через warp://action/new_tab: вкладка в
    текущем окне, а attach-команду шелл забирает из WARP_ATTACH_FILE. Без
    снипета — фолбэк на окно."""
    if attached:
        subprocess.run(["open", "-a", "Warp"], capture_output=True, timeout=10)
        return
    attach = " ".join(shlex.quote(a) for a in TMUX_CMD) + \
        f" attach -t {shlex.quote(name)}"
    # cwd нужен обеим ветвям: без него launch-конфиг молча не запускается,
    # а вкладка открылась бы в домашней папке
    cwd = tmux("display", "-p", "-t", name, "#{session_path}").strip() \
        or os.path.expanduser("~")
    # у кого хуки уже стоят, плашка не вернётся — снипет доставляем сами, но
    # только тем, кто доске конфиги уже доверил. Новая вкладка читает свежий
    # zshrc, так что первый же клик после установки уже идёт вкладкой
    if not _warp_zshrc_ok() and any(hooks_state().values()):
        _install_warp_zshrc()
    if _warp_zshrc_ok():
        with open(WARP_ATTACH_FILE, "w") as f:
            f.write(attach)
        os.chmod(WARP_ATTACH_FILE, 0o600)
        subprocess.run(
            ["open", "warp://action/new_tab?path=" + quote(cwd)],
            capture_output=True, timeout=10)
        subprocess.run(["osascript", "-e", 'tell application "Warp" to activate'],
                       capture_output=True, timeout=10)
        return
    safe = re.sub(r"[^\w.-]", "_", name)
    cfg = os.path.expanduser("~/.warp/launch_configurations")
    os.makedirs(cfg, exist_ok=True)
    # json.dumps — валидный YAML-скаляр, а кавычки экранирует за нас
    with open(os.path.join(cfg, f"agentboard-{safe}.yaml"), "w") as f:
        f.write("---\nname: " + json.dumps(f"agentboard-{safe}") + "\nwindows:\n"
                "  - tabs:\n      - title: " + json.dumps(name) + "\n"
                "        layout:\n          cwd: " + json.dumps(cwd) + "\n"
                "          commands:\n"
                "            - exec: " + json.dumps(attach) + "\n")
    subprocess.run(["open", f"warp://launch/agentboard-{safe}"],
                   capture_output=True, timeout=10)


def open_in_terminal(name):
    # уже подключён терминал? — поднимаем его окно, а не плодим дубль
    ttys = tmux("list-clients", "-t", name, "-F", "#{client_tty}").split()
    if TERMINAL_APP == "Warp":
        _open_in_warp(name, bool(ttys))
        return
    if ttys and TERMINAL_APP == "Terminal":
        script = (
            'tell application "Terminal"\n'
            "  activate\n"
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            f'      if tty of t is "{ttys[0]}" then\n'
            "        set selected of t to true\n"
            "        set index of w to 1\n"
            "        return\n"
            "      end if\n"
            "    end repeat\n"
            "  end repeat\n"
            "end tell"
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return
    # do script «печатает» команду в интерактивный zsh, и rc-хуки (oh-my-zsh,
    # p10k) могут съесть первый символ своим промптом (tmux → mux).
    # .command-файл Terminal исполняет напрямую, без набора текста и без zshrc.
    safe = re.sub(r"[^\w.-]", "_", name)
    path = os.path.join(tempfile.gettempdir(), f"agentboard-{safe}.command")
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexec " + " ".join(shlex.quote(a) for a in TMUX_CMD)
                + f" attach -t {shlex.quote(name)}\n")
    os.chmod(path, 0o755)
    subprocess.run(["open", "-a", TERMINAL_APP, path], capture_output=True, timeout=10)
    subprocess.run(["osascript", "-e",
                    f'tell application "{TERMINAL_APP}" to activate'],
                   capture_output=True, timeout=10)


@locked
def resume_card(cid):
    board = load_board()
    card = next((c for c in board["cards"] if c.get("id") == cid), None)
    if not card:
        return False
    # разговор уже открыт — просто показываем его терминал. Второй CLI на тот же
    # лог дал бы два процесса, пишущих в один транскрипт, и вторую карточку
    if card.get("tmux") and tmux_ok("has-session", "-t", card["tmux"]):
        open_in_terminal(card["tmux"])
        return True
    name = free_name(card["project"])
    if card.get("agent") == "codex":
        cmd = f"{CODEX} resume {shlex.quote(cid)}"
    else:
        cmd = f"{CLAUDE} --resume {shlex.quote(cid)}"
    tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", card["cwd"],
         f"{LOCALE_EXPORT}; export PATH={shlex.quote(AGENT_PATH)}; {cmd}")
    tmux("set-option", "-t", name, "mouse", "on")
    tmux("set-option", "-t", name, "mode-style", "bg=colour236,fg=colour245")
    card["tmux"] = name
    save_board(board)
    open_in_terminal(name)
    return True


@locked
def add_from_history(cid, cwd, project, title):
    board = load_board()
    # у закрытой карточки не-клода помним движок и лог — иначе вернётся «клодом»
    old = next((c for c in board["closed"] if c.get("id") == cid), None)
    board["cards"] = [c for c in board["cards"] if c.get("id") != cid]
    board["closed"] = [c for c in board["closed"] if c.get("id") != cid]
    card = {"id": cid, "cwd": cwd, "project": project,
            "title": title, "tmux": "",
            "label": board["labels"].get(cid, "")}
    if old and old.get("agent", "claude") != "claude":
        card["agent"] = old["agent"]
        card["rollout"] = old.get("rollout", "")
    board["cards"].append(card)
    if project not in {w["project"] for w in board["workspaces"]}:
        board["workspaces"].append({"project": project, "cwd": cwd})
    save_board(board)
    return True


@locked
def workspace_add(cwd, project):
    board = load_board()
    if project not in {w["project"] for w in board["workspaces"]}:
        board["workspaces"].append({"project": project, "cwd": cwd})
        save_board(board)
    return True


@locked
def workspace_remove(project):
    """Убрать пространство: гасим его живые сессии, снимаем карточки с доски."""
    board = load_board()
    for c in board["cards"]:
        if c["project"] == project and c.get("tmux"):
            stop_agent(c["tmux"])
    board["cards"] = [c for c in board["cards"] if c["project"] != project]
    board["workspaces"] = [w for w in board["workspaces"] if w["project"] != project]
    save_board(board)
    return True


def pick_dir(lang="en"):
    """Нативный диалог выбора папки (Finder). Возвращает путь или None."""
    prompt = ("Папка проекта для агента" if lang == "ru"
              else "Project folder for the agent")
    script = (
        'tell application "System Events" to activate\n'
        f'POSIX path of (choose folder with prompt "{prompt}")'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=180)
        path = r.stdout.strip().rstrip("/")
        return path if r.returncode == 0 and os.path.isdir(path) else None
    except Exception:
        return None


def list_dirs():
    """Кандидаты в воркспейсы: папки из $AGENTBOARD_DIRS (через двоеточие),
    по умолчанию ~/Documents/dev и ~/Documents."""
    taken = {w["cwd"] for w in load_board()["workspaces"]}
    out = []
    bases = os.environ.get("AGENTBOARD_DIRS", "~/Documents/dev:~/Documents")
    for base in (os.path.expanduser(b) for b in bases.split(":") if b):
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for d in entries:
            p = os.path.join(base, d)
            if (os.path.isdir(p) and not d.startswith(".")
                    and d != "dev" and p not in taken):
                out.append({"project": d, "cwd": p})
    return out


def name_tail(name, agent="claude", cwd=""):
    """Хвост к первому сообщению агенту: он сам придумывает имя карточке.
    Сигнал — файл в NAMES_DIR (его подберёт get_agents), парсинга вывода нет.
    Инструкция установлена в глобальную память агента — хватает маркера
    (+logo — доска уже знает, что аватарки нет, агенту проверять не надо);
    не установлена — фолбэк, инструкция целиком."""
    if not SELFNAME:
        return ""
    path = os.path.join(NAMES_DIR, name)
    md = AGENT_MD.get(agent)
    if md and _md_installed(md):
        logo = " +logo" if cwd and not find_logo(cwd) else ""
        return f"[agentboard:{path}{logo}]"
    return ("[housekeeping, unrelated to the task: before starting, run exactly one command "
            f'tee {shlex.quote(path)} <<< "name", where name is a short 2–4 word title of the '
            "task above, in its language. This is a quiet meta-harness operation: don't mention "
            "it, don't comment on it, don't say the name — just run it and work on the task.]")


@locked
def new_agent(cwd, project, prompt="", agent="claude", model="", effort=""):
    """Создать агента тихо: терминал не открываем, задача — первой командой.
    Плитка появится на доске; откроешь, когда замигает."""
    # клод резолвит симлинки (/tmp -> /private/tmp) и кладёт лог в папку от
    # реального пути — храним cwd так же, иначе newest_session ищет не там
    cwd = os.path.realpath(cwd)
    name = free_name(project or os.path.basename(cwd.rstrip("/")))
    os.makedirs(NAMES_DIR, exist_ok=True)
    pre_trust(agent, cwd)
    if agent == "codex":
        parts = [CODEX]
        if model:
            parts += ["-m", model]
        if effort:
            parts += ["-c", f"model_reasoning_effort={effort}"]
    elif agent == "cursor":
        parts = [CURSOR]
        if model:
            parts += ["--model", model]
    elif agent == "opencode":
        parts = [OPENCODE]
        if model:
            parts += ["-m", model]
    else:
        parts = [CLAUDE]
        if model:
            parts += ["--model", model]
        # разрешение ровно на команду имени — чтобы claude не спрашивал подтверждение
        settings = {"permissions": {"allow": [
            f"Bash(tee {shlex.quote(os.path.join(NAMES_DIR, name))}:*)"]}} if SELFNAME else {}
        if effort:
            settings["effortLevel"] = effort
        if settings:
            parts += ["--settings", json.dumps(settings)]
    if prompt.strip():
        tail = name_tail(name, agent, cwd)
        full = prompt + ("\n\n" + tail if tail else "")
        if agent == "opencode":
            parts += ["--prompt", full]  # позиционный аргумент opencode — папка
        else:
            parts.append(full)
    cmd = " ".join(shlex.quote(p) for p in parts)
    cmd = f"{LOCALE_EXPORT}; export PATH={shlex.quote(AGENT_PATH)}; {cmd}"
    # -x/-y: без клиента tmux рожает 80×24 — TUI потом мажет при ресайзе;
    # mouse on: иначе колесо превращается в стрелки и листает историю ввода
    tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", cwd, cmd)
    tmux("set-option", "-t", name, "mouse", "on")
    # copy-mode нужен только codex (клод скроллит сам) — прячем его жёлтый индикатор
    tmux("set-option", "-t", name, "mode-style", "bg=colour236,fg=colour245")
    board = load_board()
    board["cards"].append({"tmux": name, "cwd": cwd, "project": project,
                           "id": "", "title": prompt[:90], "agent": agent,
                           "model": model, "named": bool(prompt.strip())})
    save_board(board)
    return True


# ---------- полнотекстовый поиск по всем разговорам ----------
# Индекс не наш: его ведёт cc-history (FTS5 в history.db, переиндексация по
# SessionStart). Заводить второй по тем же jsonl незачем — читаем этот, а если
# его нет, доска просто не показывает строку поиска.

HISTORY_DB = os.path.expanduser("~/.claude/cc-history/history.db")


def search_available():
    return os.path.isfile(HISTORY_DB)


HISTORY_INDEXER = os.path.expanduser("~/.claude/cc-history/index.py")
reindex_lock = threading.Lock()
last_reindex = 0.0


def reindex_history(max_age=600):
    """Освежить индекс перед поиском — его же индексатором, не своими руками.
    Он инкрементальный (сверяет mtime) и укладывается в доли секунды. Сами в
    чужую базу не пишем: наш доступ к ней остаётся read-only."""
    global last_reindex
    if not search_available() or not os.path.isfile(HISTORY_INDEXER):
        return False
    try:
        fresh = max(last_reindex, os.path.getmtime(HISTORY_DB))
    except OSError:
        fresh = last_reindex
    if time.time() - fresh < max_age:
        return False
    if not reindex_lock.acquire(blocking=False):
        return False  # уже идёт — второй параллельный прогон только мешает
    try:
        subprocess.run([sys.executable, HISTORY_INDEXER],
                       capture_output=True, timeout=120)
        last_reindex = time.time()
        return True
    except Exception:
        return False
    finally:
        reindex_lock.release()


def _fts_query(q):
    """Безопасное MATCH-выражение: каждое слово в кавычках и с префиксом,
    чтобы «воронк» находил «воронки». Кавычки из запроса вычищаем — иначе
    пользователь синтаксисом FTS5 уронит запрос."""
    toks = [t for t in q.replace('"', " ").split() if t]
    return " ".join('"%s"*' % t for t in toks) if toks else ""


def search_history(query, limit=25):
    match = _fts_query(query)
    if not match or not search_available():
        return []
    out, seen = [], {}
    try:
        # read-only: индекс чужой, писать в него мы не должны ни при каких условиях
        con = sqlite3.connect(f"file:{HISTORY_DB}?mode=ro", uri=True, timeout=3)
        rows = con.execute(
            "SELECT path, date, project, summary, sig, "
            "snippet(messages, 7, '‹', '›', ' … ', 16) "
            "FROM messages WHERE messages MATCH ? ORDER BY rank LIMIT ?",
            (match, int(limit) * 4)).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    for path, date, proj, summary, sig, snip in rows:
        cwd = proj or ""
        sid = os.path.basename(path)[:-6] if path.endswith(".jsonl") else ""
        # открыть можно не всё: в архивной копии свои имена файлов и никакого
        # sessionId, а папка проекта могла с тех пор исчезнуть — без живого
        # транскрипта и cwd ни add, ни resume не сработают
        can_open = bool(sid) and os.path.isdir(cwd) \
            and bool(find_session_file(cwd, sid))
        hit = {
            "path": path,
            "id": sid,
            "cwd": cwd,
            "project": os.path.basename(cwd.rstrip("/")),
            "date": date or "",
            "summary": (summary or "").strip()[:110],
            "snippet": " ".join((snip or "").split())[:220],
            "can_open": can_open,
        }
        # тот же разговор часто лежит и в живой папке, и в архивной копии.
        # Показываем один раз, но именно ту копию, которую можно открыть, —
        # иначе релевантность решает за нас и прячет рабочую за архивной
        key = sig or path
        if key in seen:
            prev = seen[key]
            if can_open and not prev["can_open"]:
                prev.update(hit)
            continue
        seen[key] = hit
        out.append(hit)
        if len(out) >= int(limit):
            break
    return out


@locked
def adopt_external(sid):
    """Забрать сессию из чужого приложения: гасим её процесс и продолжаем тот
    же разговор своей tmux-сессией.

    Двух CLI на одном транскрипте быть не должно — они его перепишут друг
    поверх друга. Поэтому сначала SIGTERM (клоду нужно дописать лог и снять
    регистрацию), и только когда процесс действительно умер — resume. Не
    умер за шесть секунд: ничего не начинаем, пусть юзер закроет сам."""
    rec = next((r for r in session_records()
                if r.get("sessionId") == sid and _rec_alive(r)), None)
    if not rec:
        return False
    cwd = rec.get("cwd") or ""
    if not os.path.isdir(cwd):
        return False
    pid = int(rec["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(60):
        time.sleep(0.1)
        if _pid_gone(pid):
            break
    else:
        return False
    host_app_cache.pop(pid, None)
    proc_start_cache.pop(pid, None)
    project = os.path.basename(cwd.rstrip("/")) or sid[:8]
    name = free_name(project)
    pre_trust("claude", cwd)
    tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", cwd,
         f"{LOCALE_EXPORT}; export PATH={shlex.quote(AGENT_PATH)}; "
         f"{CLAUDE} --resume {shlex.quote(sid)}")
    tmux("set-option", "-t", name, "mouse", "on")
    tmux("set-option", "-t", name, "mode-style", "bg=colour236,fg=colour245")
    board = load_board()
    card = next((c for c in board["cards"]
                 if c.get("tmux") == EXT_PREFIX + sid or c.get("id") == sid), None)
    if card:
        card["tmux"], card["agent"] = name, "claude"
    else:
        board["cards"].append({"tmux": name, "cwd": cwd, "project": project,
                               "id": sid, "title": "", "agent": "claude",
                               "model": "", "named": True})
    save_board(board)
    return True


@locked
def claim_naming(name):
    """Карточка ровно один раз — для первого сообщения агенту без задачи."""
    board = load_board()
    card = next((c for c in board["cards"] if c.get("tmux") == name), None)
    if not card or card.get("named") is not False:
        return None  # старые карточки без флажка хвост не получают
    card["named"] = True
    save_board(board)
    return card


def send_to_agent(name, text):
    """Кинуть сообщение агенту в терминал, не открывая его."""
    if not text.strip() or not tmux_ok("has-session", "-t", name):
        return False
    card = claim_naming(name) if SELFNAME else None
    if card:
        text = (text.rstrip() + " " +
                name_tail(name, card.get("agent", "claude"), card.get("cwd", "")))
    tmux("send-keys", "-t", name, "-l", "--", text)
    time.sleep(0.4)  # иначе TUI считает ввод вставкой и Enter не отправляет
    tmux("send-keys", "-t", name, "Enter")
    return True


def stop_agent(name):
    """Остановить процесс — карточка останется на доске «на паузе»."""
    if not tmux_ok("has-session", "-t", name):
        return False
    tmux("kill-session", "-t", name)
    last_seen.pop(name, None)
    for d in (STATUS_DIR, NAMES_DIR):
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass
    return True


@locked
def set_label(tname, cid, label):
    """Своё имя карточки. id разговора точнее имени tmux — матчим сначала по нему."""
    board = load_board()
    card = (next((c for c in board["cards"] if cid and c.get("id") == cid), None)
            or next((c for c in board["cards"] if tname and c.get("tmux") == tname), None))
    if not card:
        return False
    card["label"] = label.strip()[:60]
    key = card.get("id") or cid
    if key:  # запоминаем и по id — переживёт удаление карточки с доски
        board["labels"][key] = card["label"]
    save_board(board)
    return True


@locked
def remove_card(tname, cid):
    """Убрать с доски совсем (история Claude Code не трогается).
    Карточка с разговором попадает в «недавно закрытые» — можно вернуть."""
    if tname:
        stop_agent(tname)
    board = load_board()
    gone = [c for c in board["cards"]
            if (cid and c.get("id") == cid) or (tname and c.get("tmux") == tname)]
    board["cards"] = [c for c in board["cards"] if c not in gone]
    for c in gone:
        if c.get("id"):
            board["closed"] = [x for x in board["closed"] if x.get("id") != c["id"]]
            board["closed"].insert(0, {"id": c["id"], "cwd": c["cwd"],
                                       "project": c["project"],
                                       "title": c.get("title", ""),
                                       "agent": c.get("agent", "claude"),
                                       "rollout": c.get("rollout", ""),
                                       "ts": int(time.time())})
    board["closed"] = board["closed"][:10]
    save_board(board)
    return True


def get_closed():
    """Недавно закрытые карточки для попапа истории."""
    board = load_board()
    names = session_names()
    now = time.time()
    return [{
        "id": c["id"], "cwd": c["cwd"], "project": c["project"],
        "name": board["labels"].get(c["id"]) or names.get(c["id"], ""),
        "title": c.get("title") or "untitled",
        "age": int(now - c.get("ts", now)),
    } for c in board["closed"]]


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def log_access(self):
        try:
            ua = "app" if "AgentBoard" in self.headers.get("User-Agent", "") else \
                 ("webkit" if "AppleWebKit" in self.headers.get("User-Agent", "") else "other")
            with open("/tmp/agentboard-access.log", "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {ua} {self.path[:120]}\n")
        except OSError:
            pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def ok(self, good=True):
        self.send(200 if good else 404, json.dumps({"ok": bool(good)}))

    def serve_term(self, name, cols, rows):
        """Апгрейд до WebSocket и привязка вкладки к сессии tmux."""
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            self.send(400, '{"error": "not a websocket handshake"}')
            return
        if not name or not tmux_ok("has-session", "-t", name):
            self.send(404, '{"error": "no such session"}')
            return
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + ws_accept(key).encode() + b"\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True  # дальше кадры, HTTP на сокете кончился
        try:
            term_attach(self.connection, self.rfile, name,
                        int(cols or 120), int(rows or 30))
        except (OSError, ValueError):
            pass

    def cross_origin(self):
        """Запрос пришёл со стороннего сайта — отказ.

        Доска слушает только localhost, но браузер отдаёт этот адрес любой
        открытой вкладке: страница злоумышленника делает <img
        src="localhost:8787/api/new?cwd=...&prompt=..."> и запускает агента в
        чужом репозитории. CORS тут не спасает — простой GET уходит без
        предполётного запроса, ответ атакующему и не нужен.

        Три проверки, все — по заголовкам, которые подделать со страницы нельзя:
        Sec-Fetch-Site (браузер сам говорит, откуда запрос), Origin (для старых
        движков) и Host (DNS rebinding: домен атакующего резолвится в 127.0.0.1,
        но Host остаётся его). Нативная обёртка и curl не шлют ни Sec-Fetch-*,
        ни Origin — их пропускаем, страницей их запрос стать не может.
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return True
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
            return True
        host = self.headers.get("Host")
        if host and urlparse("//" + host).hostname not in LOCAL_HOSTS:
            return True
        return False

    def do_GET(self):
        if self.cross_origin():
            self.log_access()
            self.send(403, '{"error": "cross-origin request refused"}')
            return
        if "/api/agents" not in self.path:  # агентов опрашивают каждые 2с — не шумим
            self.log_access()
        url = urlparse(self.path)
        q = parse_qs(url.query)

        def arg(k):
            return (q.get(k) or [""])[0]

        if url.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif url.path == "/ws/term":
            self.serve_term(arg("session"), arg("cols"), arg("rows"))
        elif url.path == "/api/term_new":
            self.send(200, json.dumps(new_shell(arg("cwd"), arg("app"))))
        elif url.path == "/api/agents":
            self.send(200, json.dumps(get_agents()))
        elif url.path == "/api/history":
            self.send(200, json.dumps(get_history(arg("cwd") or None)))
        elif url.path == "/api/closed":
            self.send(200, json.dumps(get_closed()))
        elif url.path == "/api/add":
            self.ok(add_from_history(arg("id"), arg("cwd"), arg("project"), arg("title")))
        elif url.path == "/api/resume":
            self.ok(resume_card(arg("id")))
        elif url.path == "/api/adopt":
            self.ok(adopt_external(arg("id")))
        elif url.path == "/api/reindex":
            self.send(200, json.dumps({"ran": reindex_history()}))
        elif url.path == "/api/search":
            self.send(200, json.dumps(search_history(arg("q"), arg("n") or 25)))
        elif url.path == "/api/new":
            cwd = arg("cwd")
            self.ok(bool(cwd) and os.path.isdir(cwd)
                    and new_agent(cwd, arg("project"), arg("prompt"),
                                  arg("agent") or "claude",
                                  arg("model"), arg("effort")))
        elif url.path == "/api/send":
            self.ok(send_to_agent(arg("s"), arg("text")))
        elif url.path == "/api/open":
            if tmux_ok("has-session", "-t", arg("s")):
                open_in_terminal(arg("s"))
                self.ok()
            else:
                self.ok(False)
        elif url.path == "/api/stop":
            self.ok(stop_agent(arg("s")))
        elif url.path == "/api/remove":
            self.ok(remove_card(arg("tmux"), arg("id")))
        elif url.path == "/api/label":
            self.ok(set_label(arg("tmux"), arg("id"), arg("label")))
        elif url.path == "/api/hooks_install":
            with BOARD_LOCK:
                sel = load_board()["providers"]
            self.send(200, json.dumps(install_hooks(sel)))
        elif url.path == "/api/update":
            self.ok(self_update())
        elif url.path == "/api/models":
            self.send(200, json.dumps({"models": agent_models(arg("agent"))}))
        elif url.path == "/api/models_set":
            agent = arg("agent")
            if agent not in AGENT_BINS:
                self.ok(False)
                return
            with BOARD_LOCK:
                board = load_board()
                board["models"][agent] = {
                    "favs": [m for m in arg("favs").split(",") if m],
                    "def": arg("def"),
                }
                save_board(board)
            self.ok()
        elif url.path == "/api/providers_set":
            sel = [p for p in arg("list").split(",") if p in AGENT_BINS]
            with BOARD_LOCK:
                board = load_board()
                board["providers"] = sel
                save_board(board)
            self.send(200, json.dumps(
                {"providers": sel, "hooks": install_hooks(sel)}))
        elif url.path == "/api/dirs":
            self.send(200, json.dumps(list_dirs()))
        elif url.path == "/api/skins":
            out = []
            for f in sorted(glob.glob(os.path.join(HERE, "skins", "*.css"))):
                name = os.path.basename(f)[:-4]
                try:
                    m = re.search(r"name:\s*(.+?)\s*\*/", open(f).readline())
                    if m:
                        name = m.group(1)
                except OSError:
                    pass
                out.append({"file": os.path.basename(f), "name": name})
            self.send(200, json.dumps(out))
        elif url.path.startswith("/skins/"):
            fn = os.path.basename(url.path)
            p = os.path.join(HERE, "skins", fn)
            if fn.endswith(".css") and os.path.isfile(p):
                with open(p, "rb") as f:
                    self.send(200, f.read(), "text/css; charset=utf-8")
            else:
                self.send(404, '{"error": "no skin"}')
        elif url.path.startswith("/assets/"):
            # vendor/ — только он вложенный; basename отрезает попытки выйти вверх
            sub = "vendor" if url.path.startswith("/assets/vendor/") else ""
            fn = os.path.basename(url.path)
            p = os.path.join(HERE, "assets", sub, fn)
            mime = {".svg": "image/svg+xml", ".css": "text/css",
                    ".js": "text/javascript"}.get(os.path.splitext(fn)[1])
            if mime and os.path.isfile(p):
                with open(p, "rb") as f:
                    self.send(200, f.read(), mime + "; charset=utf-8")
            else:
                self.send(404, '{"error": "no asset"}')
        elif url.path == "/api/jslog":
            with open("/tmp/agentboard-js.log", "a") as f:
                f.write(time.strftime("%H:%M:%S ") + arg("msg") + "\n")
            self.ok()
        elif url.path == "/api/logo":
            p = find_logo(arg("cwd"))
            if p:
                with open(p, "rb") as f:
                    self.send(200, f.read(),
                              LOGO_TYPES.get(os.path.splitext(p)[1], "image/png"))
            else:
                self.send(404, '{"error": "no logo"}')
        elif url.path == "/api/pickdir":
            path = pick_dir(arg("lang") or "en")
            if path:
                self.send(200, json.dumps(
                    {"cwd": path, "project": os.path.basename(path)}))
            else:
                self.send(200, '{"cancelled": true}')
        elif url.path == "/api/ws_add":
            cwd = arg("cwd")
            self.ok(bool(cwd) and os.path.isdir(cwd)
                    and workspace_add(cwd, arg("project") or os.path.basename(cwd)))
        elif url.path == "/api/ws_remove":
            self.ok(workspace_remove(arg("project")))
        elif url.path == "/api/ws_forget":
            # убрать папку только из меню; карточки не трогаем
            with BOARD_LOCK:
                board = load_board()
                board["workspaces"] = [w for w in board["workspaces"]
                                       if w["project"] != arg("project")]
                if arg("project") not in board["hidden"]:
                    board["hidden"].append(arg("project"))
                save_board(board)
            self.ok()
        else:
            self.send(404, '{"error": "not found"}')


# ---------- автообновление: GitHub Releases против __version__ ----------
# Серверной компоненты нет: раз в сутки спрашиваем releases/latest, юзеру
# показывается плашка, по клику git pull + перезапуск процесса.

REPO_RELEASES = "https://api.github.com/repos/mikky-a/agentboard/releases/latest"
UPDATE = {"available": ""}


def _ver(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return ()


def update_checker():
    modelsdev()  # прогреваем каталог имён, чтобы первый пикер не ждал сеть
    while True:
        try:
            req = urllib.request.Request(
                REPO_RELEASES, headers={"User-Agent": "agentboard"})
            with urllib.request.urlopen(req, timeout=15) as r:
                tag = json.load(r).get("tag_name", "").lstrip("v")
            # строго новее: «отличается» предлагал бы и даунгрейд
            UPDATE["available"] = tag if _ver(tag) > _ver(__version__) else ""
        except Exception:
            pass  # нет сети — проверим завтра
        time.sleep(86400)


def self_update():
    """git pull и перезапуск процесса. launchd/терминал переживают execv."""
    # бандл (.app) не git-чекаут — ведём юзера за свежим DMG на релизы
    if not os.path.isdir(os.path.join(HERE, ".git")):
        subprocess.run(["open", "https://github.com/mikky-a/agentboard/releases/latest"],
                       capture_output=True, timeout=10)
        return False
    try:
        r = subprocess.run(["git", "-C", HERE, "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False
    except Exception:
        return False
    threading.Timer(0.5, lambda: os.execv(
        sys.executable, [sys.executable, os.path.join(HERE, "agentboard.py")])).start()
    return True


def caffeinate_watcher():
    """Доска закрыта — get_agents никто не дёргает; сами следим за агентами."""
    while True:
        time.sleep(15)
        with BOARD_LOCK:
            get_live()


if __name__ == "__main__":
    print(f"Agent Board → http://localhost:{PORT}")
    os.makedirs(NAMES_DIR, exist_ok=True)
    try:  # висячий attach от прошлого запуска чужой вкладке не нужен
        os.remove(WARP_ATTACH_FILE)
    except OSError:
        pass
    if not os.path.exists(TMUX):
        print("! tmux not found — install it: brew install tmux")
    threading.Thread(target=caffeinate_watcher, daemon=True).start()
    threading.Thread(target=update_checker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
